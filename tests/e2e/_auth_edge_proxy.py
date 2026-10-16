"""Loopback stand-in for an authenticating front door such as the Databricks Apps edge.

Every ``/v1/*`` HTTP request and WebSocket upgrade must carry a credential the
upstream server accepts (checked with ``GET /v1/me``); anything else is answered
``401`` here and never reaches the server. The unauthenticated bootstrap surface
(``/v1/me``, ``/v1/info``) is exempt so a browser can reach the login UI, exactly
as a real SSO front door redirects to its own login before the app loads.
Accepted traffic is relayed unchanged,
so a WebSocket authenticated at upgrade time stays open however long it lives —
exactly how a long-lived host tunnel outlives the credential it connected with.
"""

from __future__ import annotations

import asyncio
import contextlib
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect
from websockets.asyncio.client import connect as ws_connect

from omnigent.host.identity import HOST_AUTH_REQUIRED_HEADER
from tests._helpers.live_server import find_free_port

_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "accept-encoding",
    }
)
_WS_HANDSHAKE_ONLY = frozenset(
    {
        "host",
        "connection",
        "upgrade",
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-extensions",
        "sec-websocket-protocol",
        "sec-websocket-accept",
    }
)
_AUTH_HEADERS = ("authorization", "cookie")
_UNAUTHORIZED_BODY = {"error": "Invalid Token"}
# The unauthenticated bootstrap surface a browser needs to render the login UI,
# passed through like a real SSO front door's own login page. The runner never
# calls these; it hits the gated /token and /tunnel.
_UNGATED_V1 = frozenset({"/v1/me", "/v1/info"})


def _sendable_close_code(code: int | None) -> int:
    """Clamp a relayed WebSocket close code to one the peer accepts.

    1005/1006/1015 are reserved sentinels the stack sets internally and never
    sends on the wire; relaying one raises, so fall back to a normal 1000 close.
    """
    if code is None or not (1000 <= code < 5000) or code in (1005, 1006, 1015):
        return 1000
    return code


@dataclass
class EdgeRequest:
    """One request seen by the edge.

    :param method: HTTP method, or ``"WS"`` for a WebSocket upgrade.
    :param path: Request path, e.g. ``"/v1/runners/runner_abc/tunnel"``.
    :param status: Status the edge answered (``101`` for an accepted upgrade).
    :param authorized: Whether the credential check passed (``None`` when the
        path is not gated).
    :param at: Wall-clock time the request was seen.
    """

    method: str
    path: str
    status: int
    authorized: bool | None
    at: float = field(default_factory=time.time)


class AuthEdgeProxy:
    """Reverse proxy that gates ``/v1/*`` on a credential the upstream accepts.

    :param upstream: Base URL of the real server, e.g. ``"http://127.0.0.1:8123"``.
    """

    def __init__(self, upstream: str) -> None:
        self._upstream = upstream.rstrip("/")
        self._port = find_free_port()
        self.requests: list[EdgeRequest] = []
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self._client: httpx.AsyncClient | None = None

    @property
    def url(self) -> str:
        """Base URL clients should use instead of the upstream server."""
        return f"http://127.0.0.1:{self._port}"

    def rejections(self, path_suffix: str) -> list[EdgeRequest]:
        """Return the 401s the edge answered for paths ending in *path_suffix*."""
        return [r for r in self.requests if r.status == 401 and r.path.endswith(path_suffix)]

    def start(self) -> str:
        """Start serving in a background thread and return the edge URL."""

        @contextlib.asynccontextmanager
        async def lifespan(_app: Starlette) -> AsyncIterator[None]:
            try:
                yield
            finally:
                await self._aclose_client()

        app = Starlette(
            routes=[
                Route(
                    "/{path:path}",
                    self._relay_http,
                    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
                ),
                WebSocketRoute("/{path:path}", self._relay_websocket),
            ],
            lifespan=lifespan,
        )
        config = uvicorn.Config(
            app, host="127.0.0.1", port=self._port, log_level="warning", ws="websockets"
        )
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if self._server.started:
                return self.url
            time.sleep(0.05)
        raise RuntimeError("auth edge proxy did not start")

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)

    async def _aclose_client(self) -> None:
        """Close the upstream probe/relay client on the server's own loop."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._upstream,
                timeout=httpx.Timeout(30.0, read=None),
                trust_env=False,
            )
        # A real front door relays the client's own cookie and never injects one
        # of its own; dropping any stored Set-Cookie keeps a login made through
        # the edge from authenticating a later un-credentialed request.
        self._client.cookies.clear()
        return self._client

    def _gated(self, path: str) -> bool:
        return path.startswith("/v1/") and path not in _UNGATED_V1

    async def _authorized(self, headers: Request | WebSocket) -> bool:
        forwarded = {
            name: value for name, value in headers.headers.items() if name.lower() in _AUTH_HEADERS
        }
        if not forwarded:
            return False
        try:
            probe = await self._http().get("/v1/me", headers=forwarded)
        except httpx.HTTPError:
            return False
        return probe.status_code == 200

    def _record(self, method: str, path: str, status: int, authorized: bool | None) -> None:
        self.requests.append(
            EdgeRequest(method=method, path=path, status=status, authorized=authorized)
        )

    async def _relay_http(self, request: Request) -> Response:
        path = request.url.path
        authorized: bool | None = None
        if self._gated(path):
            authorized = await self._authorized(request)
            if not authorized:
                self._record(request.method, path, 401, authorized)
                return JSONResponse(_UNAUTHORIZED_BODY, status_code=401)
        headers = {k: v for k, v in request.headers.items() if k.lower() not in _HOP_BY_HOP}
        target = path + (f"?{request.url.query}" if request.url.query else "")
        upstream_req = self._http().build_request(
            request.method, target, headers=headers, content=request.stream()
        )
        try:
            upstream = await self._http().send(upstream_req, stream=True)
        except httpx.HTTPError as exc:
            # Upstream unreachable or dropped mid-request; record it and fail
            # cleanly so rejection/summary assertions point at the real cause.
            self._record(request.method, path, 502, authorized)
            return JSONResponse({"error": f"edge upstream failure: {exc}"}, status_code=502)
        self._record(request.method, path, upstream.status_code, authorized)
        response_headers = {
            k: v for k, v in upstream.headers.items() if k.lower() not in _HOP_BY_HOP
        }
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers=response_headers,
            background=_CloseUpstream(upstream),
        )

    async def _relay_websocket(self, websocket: WebSocket) -> None:
        path = websocket.url.path
        authorized: bool | None = None
        if self._gated(path):
            authorized = await self._authorized(websocket)
            if not authorized:
                self._record("WS", path, 401, authorized)
                await websocket.send_denial_response(
                    JSONResponse(_UNAUTHORIZED_BODY, status_code=401)
                )
                return
        headers = {
            k: v for k, v in websocket.headers.items() if k.lower() not in _WS_HANDSHAKE_ONLY
        }
        target = self._upstream.replace("http://", "ws://", 1) + path
        if websocket.url.query:
            target += f"?{websocket.url.query}"
        try:
            upstream = await ws_connect(
                target, additional_headers=headers, max_size=None, open_timeout=30
            )
        except websockets.exceptions.InvalidStatus as exc:
            status = exc.response.status_code
            self._record("WS", path, status, authorized)
            await websocket.send_denial_response(
                Response(exc.response.body or b"", status_code=status)
            )
            return
        except (OSError, TimeoutError, websockets.exceptions.WebSocketException) as exc:
            # Upstream never completed the handshake (refused, timed out, or a
            # protocol error); fail the upgrade instead of crashing the edge.
            self._record("WS", path, 502, authorized)
            await websocket.send_denial_response(
                Response(f"edge upstream connect failed: {exc}".encode(), status_code=502)
            )
            return
        self._record("WS", path, 101, authorized)
        # Relay the server's auth-mode signal to the host, as a real front
        # door forwards the upgrade response headers it receives upstream.
        upstream_response = upstream.response
        signalled = (
            upstream_response.headers.get(HOST_AUTH_REQUIRED_HEADER)
            if upstream_response is not None
            else None
        )
        accept_headers = (
            [(HOST_AUTH_REQUIRED_HEADER.encode(), signalled.encode())]
            if signalled is not None
            else []
        )
        await websocket.accept(headers=accept_headers)

        async def client_to_upstream() -> None:
            try:
                while True:
                    message = await websocket.receive()
                    if message["type"] == "websocket.disconnect":
                        await upstream.close(code=_sendable_close_code(message.get("code")))
                        return
                    if message.get("text") is not None:
                        await upstream.send(message["text"])
                    elif message.get("bytes") is not None:
                        await upstream.send(message["bytes"])
            except (WebSocketDisconnect, websockets.exceptions.ConnectionClosed):
                with contextlib.suppress(Exception):
                    await upstream.close()

        async def upstream_to_client() -> None:
            try:
                async for frame in upstream:
                    if isinstance(frame, str):
                        await websocket.send_text(frame)
                    else:
                        await websocket.send_bytes(frame)
            except websockets.exceptions.ConnectionClosed:
                pass
            finally:
                with contextlib.suppress(Exception):
                    await websocket.close(code=_sendable_close_code(upstream.close_code))

        pumps = [
            asyncio.create_task(client_to_upstream()),
            asyncio.create_task(upstream_to_client()),
        ]
        try:
            await asyncio.wait(pumps, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in pumps:
                task.cancel()
            with contextlib.suppress(Exception):
                await asyncio.gather(*pumps, return_exceptions=True)
            with contextlib.suppress(Exception):
                await upstream.close()


class _CloseUpstream:
    """Starlette background task closing the streamed upstream response."""

    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    async def __call__(self) -> None:
        await self._response.aclose()
