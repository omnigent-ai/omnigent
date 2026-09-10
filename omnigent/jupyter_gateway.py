"""Fixed-session Jupyter HTTP and kernel channels over existing runner transport."""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from starlette.requests import HTTPConnection
from websockets.exceptions import ConnectionClosed

from omnigent.errors import OmnigentError

MAX_BYTES = 32_000_000
SESSION = re.compile(r"[A-Za-z0-9_-]{1,128}")
REQUEST_HEADERS = {
    "accept",
    "accept-encoding",
    "content-type",
    "if-none-match",
    "if-modified-since",
}
RESPONSE_HEADERS = {
    "content-type",
    "content-length",
    "content-encoding",
    "content-disposition",
    "cache-control",
    "etag",
    "last-modified",
    "vary",
}


def require_jupyter_origin(connection: HTTPConnection) -> None:
    """Reject cross-origin writes before their body or assigned runner is read."""
    expected = str(connection.url).split("/", 3)
    scheme = {"ws:": "http:", "wss:": "https:"}.get(expected[0], expected[0])
    origin = connection.headers.get("origin")
    if origin is None and connection.headers.get("authorization", "").startswith("Bearer "):
        return
    if origin != scheme + "//" + expected[2]:
        raise HTTPException(403, "Jupyter requires a same-origin request")


async def _body(request: Request) -> bytes:
    if request.headers.get("content-encoding", "identity").lower() != "identity":
        raise HTTPException(415, "Compressed Jupyter requests are not supported")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_BYTES:
            raise HTTPException(413, "Jupyter request exceeds the size limit")
        chunks.append(chunk)
    return b"".join(chunks)


async def _shuttle(browser: WebSocket, upstream: Any) -> None:
    async def outbound() -> None:
        while True:
            event = await browser.receive()
            if event["type"] == "websocket.disconnect":
                return
            data = event.get("text") if event.get("text") is not None else event.get("bytes")
            if data is not None:
                if len(data.encode() if isinstance(data, str) else data) > MAX_BYTES:
                    await browser.close(1009, "Kernel message exceeds the size limit")
                    return
                await upstream.send(data)

    async def inbound() -> None:
        while True:
            data = await upstream.recv()
            if len(data.encode() if isinstance(data, str) else data) > MAX_BYTES:
                await browser.close(1009, "Kernel message exceeds the size limit")
                return
            if isinstance(data, bytes):
                await browser.send_bytes(data)
            else:
                await browser.send_text(data)

    tasks = {asyncio.create_task(outbound()), asyncio.create_task(inbound())}
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            with contextlib.suppress(ConnectionClosed, WebSocketDisconnect):
                task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def jupyter_gateway_router(
    authorize: Callable[[HTTPConnection, str], Awaitable[None]],
    get_client: Callable[[str], Awaitable[httpx.AsyncClient]],
    websocket_factory: Callable[[str, str], Any],
    *,
    prefix: str = "/v1",
    trusted_tunnel: bool = False,
) -> APIRouter:
    router = APIRouter(prefix=prefix + "/sessions/{session_id}/docloop")

    async def authorized(connection: HTTPConnection, session_id: str) -> None:
        await authorize(connection, session_id)
        if not SESSION.fullmatch(session_id):
            raise HTTPException(400, "Invalid session identity")

    async def forward(request: Request, session_id: str, path: str = "") -> StreamingResponse:
        await authorized(request, session_id)
        if request.method not in {"GET", "HEAD"}:
            if not (trusted_tunnel and request.client and request.client.host == "tunnel"):
                require_jupyter_origin(request)
        if path.startswith("/") or "\\" in path or any(p in {".", ".."} for p in path.split("/")):
            raise HTTPException(400, "Invalid Jupyter path")
        body = await _body(request)
        base = f"/v1/sessions/{session_id}/docloop/jupyter"
        target = base + ("/" + path if request.url.path.endswith("/") or path else "")
        if request.url.query:
            target += "?" + request.url.query
        try:
            client = await get_client(session_id)
            headers = {k: v for k, v in request.headers.items() if k.lower() in REQUEST_HEADERS}
            response = await client.send(
                client.build_request(
                    request.method, target, headers=headers, content=body, timeout=50
                ),
                stream=True,
                follow_redirects=False,
            )
        except Exception as exc:
            raise HTTPException(503, "Jupyter runner is unavailable") from exc
        output = {k: v for k, v in response.headers.items() if k.lower() in RESPONSE_HEADERS}
        location = response.headers.get("location")
        if location:
            parsed = urlsplit(location)
            if parsed.scheme or parsed.netloc or not parsed.path.startswith(base + "/"):
                await response.aclose()
                raise HTTPException(502, "Unexpected Jupyter redirect")
            output["Location"] = location
        output.setdefault("cache-control", "no-store")
        output["Content-Security-Policy"] = "frame-ancestors 'self'"

        async def content() -> AsyncIterator[bytes]:
            size = 0
            try:
                async for chunk in response.aiter_raw():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise ValueError("Jupyter response exceeds the size limit")
                    yield chunk
            finally:
                await response.aclose()

        return StreamingResponse(content(), status_code=response.status_code, headers=output)

    router.add_api_route("/jupyter", forward, methods=["GET", "HEAD"])
    router.add_api_route(
        "/jupyter/{path:path}", forward, methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"]
    )

    @router.websocket("/jupyter/api/kernels/{kernel_id}/channels")
    async def channels(websocket: WebSocket, session_id: str, kernel_id: str) -> None:
        try:
            await authorized(websocket, session_id)
            if not (trusted_tunnel and websocket.client and websocket.client.host == "tunnel"):
                require_jupyter_origin(websocket)
            if not SESSION.fullmatch(kernel_id):
                raise HTTPException(400, "Invalid kernel identity")
            target = f"/v1/sessions/{session_id}/docloop/jupyter/api/kernels/{kernel_id}/channels"
            if websocket.url.query:
                target += "?" + websocket.url.query
            async with websocket_factory(session_id, target) as upstream:
                await websocket.accept()
                await _shuttle(websocket, upstream)
        except (HTTPException, OmnigentError):
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(1008, "Jupyter access unavailable")
        except Exception:  # noqa: BLE001 — transport errors can contain private endpoints.
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(1011, "Jupyter channel unavailable")
        finally:
            with contextlib.suppress(RuntimeError, WebSocketDisconnect):
                await websocket.close(1000)

    return router
