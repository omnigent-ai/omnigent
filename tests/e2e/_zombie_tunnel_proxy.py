"""A WebSocket-aware stand-in for a front door that outlives the backend request.

Forwards a host daemon's plain HTTP requests and its ``/v1/hosts/{id}/tunnel``
WebSocket to the real server. ``sever(zombie=True)`` ends only the server-side
leg: the server runs its disconnect path, while the host-facing WebSocket stays
open and keeps answering protocol PINGs with PONGs (the websockets server does
that below the application), so no application frame reaches the host again.
``sever(zombie=False)`` closes both legs — the clean cut a daemon already
survives — and serves as the control.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar
from urllib.parse import urlparse

import httpx
from websockets.asyncio.client import ClientConnection, connect
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

_T = TypeVar("_T")
_logger = logging.getLogger(__name__)

_MAX_FRAME_BYTES = 100 * 1024 * 1024
# Hop-by-hop and handshake headers the proxy regenerates on its own legs.
_HOP_HEADERS = frozenset(
    {
        "host",
        "connection",
        "upgrade",
        "content-length",
        "user-agent",
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-extensions",
        "sec-websocket-protocol",
    }
)


def _forwardable(headers: Headers) -> dict[str, str]:
    return {k: v for k, v in headers.raw_items() if k.lower() not in _HOP_HEADERS}


class _Link:
    """One forwarded tunnel: the host-facing leg and the server-facing leg."""

    def __init__(self, host_side: ServerConnection, server_side: ClientConnection) -> None:
        self.host_side = host_side
        self.server_side = server_side
        self.zombie = False

    async def run(self) -> None:
        to_server = asyncio.create_task(self._pump_host_frames())
        to_host = asyncio.create_task(self._pump_server_frames())
        try:
            await asyncio.wait({to_server, to_host}, return_when=asyncio.FIRST_COMPLETED)
            if self.zombie:
                # The server leg is gone; keep the host leg open and drained so
                # its protocol pings keep being answered until the host gives up.
                try:
                    await to_server
                except Exception:
                    _logger.exception("zombie host leg pump failed; closing the host leg")
                    with contextlib.suppress(Exception):
                        await self.host_side.close()
        finally:
            for task in (to_server, to_host):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            with contextlib.suppress(Exception):
                await self.server_side.close()
            if not self.zombie:
                with contextlib.suppress(Exception):
                    await self.host_side.close()

    async def _pump_host_frames(self) -> None:
        with contextlib.suppress(ConnectionClosed):
            async for message in self.host_side:
                if self.zombie:
                    continue
                await self.server_side.send(message)

    async def _pump_server_frames(self) -> None:
        with contextlib.suppress(ConnectionClosed):
            async for message in self.server_side:
                if self.zombie:
                    return
                await self.host_side.send(message)

    async def sever(self, *, zombie: bool) -> None:
        self.zombie = zombie
        await self.server_side.close(code=1001, reason="front door ended the backend request")
        if not zombie:
            await self.host_side.close(code=1001, reason="front door ended the request")


class ZombieTunnelProxy:
    """Run the stand-in front door on its own event loop thread.

    :param upstream_url: The real server, e.g. ``"http://127.0.0.1:18501"``.
    """

    def __init__(self, upstream_url: str) -> None:
        self._upstream = upstream_url.rstrip("/")
        parsed = urlparse(self._upstream)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        self._ws_upstream = f"{scheme}://{parsed.netloc}"
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="zombie-tunnel-proxy", daemon=True
        )
        self._server: Server | None = None
        self._port = 0
        self._links: list[_Link] = []
        self._lock = threading.Lock()
        self._tunnel_open = threading.Event()

    @property
    def url(self) -> str:
        """The URL a host daemon should be pointed at, e.g. ``"http://127.0.0.1:43123"``."""
        return f"http://127.0.0.1:{self._port}"

    def __enter__(self) -> ZombieTunnelProxy:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> None:
        self._thread.start()
        try:
            self._server = self._run(self._start())
            self._port = self._server.sockets[0].getsockname()[1]
        except Exception:
            self.stop()
            raise

    def stop(self) -> None:
        if self._loop.is_closed():
            return
        if self._server is not None:
            with contextlib.suppress(Exception):
                self._run(self._stop())
            self._server = None
        self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread.is_alive():
            self._thread.join(timeout=10)
            if self._thread.is_alive():
                raise RuntimeError("zombie tunnel proxy loop thread did not stop within 10s")
        self._loop.close()

    def wait_for_tunnel(self, timeout: float = 60.0) -> None:
        """Block until at least one host tunnel has been forwarded upstream."""
        if not self._tunnel_open.wait(timeout):
            raise AssertionError(f"no host tunnel was forwarded through the proxy in {timeout}s")

    def sever(self, *, zombie: bool) -> int:
        """End the server-side leg of every forwarded tunnel.

        :param zombie: Keep the host-facing leg open and ping-answering (the
            reported hypothesis) instead of closing it too (the control).
        :returns: Number of tunnels severed.
        """
        return self._run(self._sever(zombie))

    def _run(self, coro: Coroutine[Any, Any, _T], timeout: float = 30.0) -> _T:
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    async def _start(self) -> Server:
        return await serve(
            self._handle,
            "127.0.0.1",
            0,
            process_request=self._forward_http,
            ping_interval=None,
            max_size=_MAX_FRAME_BYTES,
            compression=None,
        )

    async def _stop(self) -> None:
        assert self._server is not None
        with self._lock:
            links = list(self._links)
        for link in links:
            link.zombie = False
            with contextlib.suppress(Exception):
                await link.host_side.close()
            with contextlib.suppress(Exception):
                await link.server_side.close()
        self._server.close()
        await self._server.wait_closed()

    async def _sever(self, zombie: bool) -> int:
        with self._lock:
            links = list(self._links)
        for link in links:
            await link.sever(zombie=zombie)
        return len(links)

    async def _forward_http(
        self, connection: ServerConnection, request: Request
    ) -> Response | None:
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return None
        # Plain GETs (the CLI's auth probe, /v1/me, host status) pass through so
        # the front door looks like the server to the daemon and the CLI;
        # websockets rejects any other method before this hook runs.
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=30.0) as client:
                upstream = await client.get(
                    f"{self._upstream}{request.path}", headers=_forwardable(request.headers)
                )
        except httpx.HTTPError as exc:
            return Response(502, "Bad Gateway", Headers(), str(exc).encode())
        headers = Headers()
        for key, value in upstream.headers.items():
            if key.lower() not in {
                "content-length",
                "transfer-encoding",
                "connection",
                "content-encoding",
            }:
                headers[key] = value
        headers["Content-Length"] = str(len(upstream.content))
        return Response(upstream.status_code, upstream.reason_phrase, headers, upstream.content)

    async def _handle(self, host_side: ServerConnection) -> None:
        request = host_side.request
        assert request is not None
        try:
            server_side = await connect(
                f"{self._ws_upstream}{request.path}",
                additional_headers=_forwardable(request.headers),
                ping_interval=None,
                max_size=_MAX_FRAME_BYTES,
                compression=None,
                open_timeout=30,
            )
        except Exception as exc:
            await host_side.close(code=1011, reason=f"upstream connect failed: {exc}"[:120])
            return
        link = _Link(host_side, server_side)
        with self._lock:
            self._links.append(link)
        self._tunnel_open.set()
        try:
            await link.run()
        finally:
            with self._lock:
                self._links.remove(link)
