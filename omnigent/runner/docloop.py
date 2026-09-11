"""Notebook forwarding through the session's existing runner and harness clients."""

from __future__ import annotations

import asyncio
import hmac

import httpx
from fastapi import APIRouter, HTTPException, Request
from starlette.requests import HTTPConnection

from omnigent.docloop_gateway import (
    Method,
    NotebookReply,
    NotebookUnavailable,
    notebook_gateway_router,
)
from omnigent.errors import OmnigentError
from omnigent.runner.routing import RunnerRouter
from omnigent.runtime.harnesses.process_manager import HarnessProcessManager, NoLiveHarnessError


async def _forward(
    client: httpx.AsyncClient,
    session_id: str,
    method: Method,
    body: bytes,
    *,
    max_response_bytes: int,
) -> NotebookReply:
    # Credentials belong to the existing client; browser headers never cross a hop.
    headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
    if method in {"PATCH", "POST"}:
        headers.update({"Content-Type": "application/json", "X-Docloop-Edit": "1"})
    async with client.stream(
        method,
        f"/v1/sessions/{session_id}/docloop/"
        + ("recover-history" if method == "POST" else "document"),
        content=body,
        headers=headers,
        follow_redirects=False,
        timeout=12.0,
    ) as response:
        # Read raw bytes so compressed replies cannot inflate beyond the limit.
        if response.headers.get("content-encoding", "identity").lower() != "identity":
            raise ValueError("Compressed notebook response")
        declared = response.headers.get("content-length")
        if declared is not None and (
            not declared.isascii()
            or not declared.isdigit()
            or len(declared) > 16
            or int(declared) > max_response_bytes
        ):
            raise ValueError("Invalid notebook response length")
        chunks: list[bytes] = []
        size = 0
        async for chunk in response.aiter_raw():
            size += len(chunk)
            if size > max_response_bytes:
                raise ValueError("Notebook response exceeds limit")
            chunks.append(chunk)
        if declared is not None and int(declared) != size:
            raise ValueError("Incomplete notebook response")
        return NotebookReply(session_id, response.status_code, b"".join(chunks))


class AssignedRunnerNotebookTransport:
    """The central server forwards through existing session affinity only."""

    def __init__(self, router: RunnerRouter | None) -> None:
        self.router = router

    async def jupyter_client(self, session_id: str) -> httpx.AsyncClient:
        if self.router is None:
            raise NotebookUnavailable
        routed = await asyncio.to_thread(self.router.client_for_session_resources, session_id)
        return routed.client

    def jupyter_channels(self, _session_id: str, path: str):
        from omnigent.runtime import get_runner_ws_factory

        factory = get_runner_ws_factory()
        if factory is None:
            raise NotebookUnavailable
        return factory(path)

    async def forward_notebook(
        self, session_id: str, method: Method, body: bytes, *, max_response_bytes: int
    ) -> NotebookReply:
        if self.router is None:
            raise NotebookUnavailable
        try:
            routed = await asyncio.to_thread(self.router.client_for_session_resources, session_id)
        except OmnigentError as exc:
            raise NotebookUnavailable from exc
        return await _forward(
            routed.client, session_id, method, body, max_response_bytes=max_response_bytes
        )


class LiveHarnessNotebookTransport:
    """The runner reuses a live harness without starting or replacing it."""

    def __init__(self, manager: HarnessProcessManager | None) -> None:
        self.manager = manager

    async def jupyter_client(self, session_id: str) -> httpx.AsyncClient:
        if self.manager is None:
            raise NotebookUnavailable
        return await self.manager.get_client(session_id, "any")

    def jupyter_channels(self, session_id: str, path: str):
        if self.manager is None:
            raise NotebookUnavailable
        return self.manager.jupyter_channels(session_id, path)

    async def forward_notebook(
        self, session_id: str, method: Method, body: bytes, *, max_response_bytes: int
    ) -> NotebookReply:
        if self.manager is None:
            raise NotebookUnavailable
        try:
            client = await self.manager.get_client(session_id, "any")
        except NoLiveHarnessError as exc:
            raise NotebookUnavailable from exc
        return await _forward(
            client, session_id, method, body, max_response_bytes=max_response_bytes
        )


def runner_notebook_router(
    manager: HarnessProcessManager | None, auth_token: str | None = None
) -> APIRouter:
    """Registered behind the runner app's existing authenticated tunnel boundary."""

    async def authorize(_request: Request, _session_id: str) -> None:
        pass

    from omnigent.jupyter_gateway import jupyter_gateway_router
    from omnigent.notebook_history_gateway import notebook_history_router

    async def authorize_jupyter(connection: HTTPConnection, _session_id: str) -> None:
        if connection.client and connection.client.host == "tunnel":
            return
        provided = connection.headers.get("authorization", "")
        if not auth_token or not hmac.compare_digest(provided, "Bearer " + auth_token):
            raise HTTPException(401, "Runner authentication required")

    transport = LiveHarnessNotebookTransport(manager)
    router = notebook_gateway_router(authorize, transport)
    router.include_router(
        notebook_history_router(authorize_jupyter, transport.jupyter_client, prefix="")
    )
    router.include_router(
        jupyter_gateway_router(
            authorize_jupyter,
            transport.jupyter_client,
            transport.jupyter_channels,
            prefix="",
            trusted_tunnel=True,
        )
    )
    return router
