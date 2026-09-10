"""Notebook forwarding through the session's existing runner and harness clients."""

from __future__ import annotations

import asyncio

import httpx
from fastapi import APIRouter, Request

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
    if method == "PATCH":
        headers.update({"Content-Type": "application/json", "X-Docloop-Edit": "1"})
    async with client.stream(
        method,
        f"/v1/sessions/{session_id}/docloop/document",
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


def runner_notebook_router(manager: HarnessProcessManager | None) -> APIRouter:
    """Registered behind the runner app's existing authenticated tunnel boundary."""

    async def authorize(_request: Request, _session_id: str) -> None:
        pass

    return notebook_gateway_router(authorize, LiveHarnessNotebookTransport(manager))
