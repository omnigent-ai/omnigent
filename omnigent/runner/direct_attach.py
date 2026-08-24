"""Loopback listener for relay-free browser terminal attach.

The runner normally reaches browsers only through the server relay:
browser → server WS → runner tunnel → tmux, which costs two WAN round
trips per keystroke even when the browser and the runner share a
machine. This module serves the runner's *existing* terminal-attach
handler on ``127.0.0.1`` so a same-machine browser can attach with
zero WAN legs. The server learns the endpoint from the tunnel hello
advert and hands owners a ``direct_attach_url``; the web client
probes it and silently falls back to the relay when unreachable
(other machines, Safari's mixed-content policy, listener down).

Security model
--------------

- Binds ``127.0.0.1`` only — never a routable interface.
- Every handshake must present the per-process bearer token
  (``?token=``, high-entropy, held only in runner memory, the tunnel
  hello, and the owner-gated terminals API response).
- Browser handshakes must carry an allow-listed ``Origin`` (the
  server origin the runner is tunneled to); requests without an
  ``Origin`` header (curl, native tools) pass on the token alone.
  This blocks DNS-rebinding-style pages, which present a foreign
  origin, from driving the port even if a token ever leaked.
- Authorization-wise the token equals owner-level write attach: the
  server discloses it only to callers who hold ``LEVEL_OWNER`` on the
  session, mirroring the relay's write-attach gate.

CORS never applies on this path: WebSocket handshakes are exempt from
CORS preflight, and the client performs no HTTP fetches against the
listener — reachability is tested via the ``/probe`` WebSocket route,
which validates and closes without touching any terminal.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from collections.abc import Awaitable, Callable
from typing import Final
from urllib.parse import urlsplit

import uvicorn
from fastapi import FastAPI, Query, WebSocket
from starlette import status

from omnigent.debug_logging import runner_primary_session_id

_logger = logging.getLogger(__name__)

# Bind loopback by literal IPv4 address, matching the URL the server
# composes for browsers. "localhost" is avoided: it can resolve to ::1
# and leave the advertised 127.0.0.1 URL pointing at nothing.
DIRECT_ATTACH_BIND_HOST: Final[str] = "127.0.0.1"

# Bound on how long a listener startup may take before the runner gives
# up and continues relay-only. Startup is a local socket bind; seconds
# means something is wrong, and the listener must never delay the tunnel.
_STARTUP_TIMEOUT_S: Final[float] = 5.0
_STARTUP_POLL_S: Final[float] = 0.01

_SHUTDOWN_TIMEOUT_S: Final[float] = 2.0

# The attach handler contract shared with ``runner/app.py``'s
# ``terminal_resource_attach_ws`` route.
AttachHandler = Callable[..., Awaitable[None]]


def new_direct_attach_token() -> str:
    """Return a fresh high-entropy, URL-safe direct-attach bearer token."""
    return secrets.token_urlsafe(32)


def allowed_origin_for_server(server_url: str) -> str | None:
    """Derive the browser Origin allowed on the listener from *server_url*.

    The standalone web UI is served by the omnigent server itself, so a
    legitimate browser handshake carries that server's origin. Path,
    query, and userinfo are dropped; scheme and host:port are kept.

    :param server_url: The runner's server base URL, e.g.
        ``"https://omnigents.example.databricksapps.com"`` or
        ``"http://127.0.0.1:6767"``.
    :returns: The origin string (``scheme://host[:port]``), or ``None``
        when *server_url* has no usable scheme/host (origin checks then
        reject all browser handshakes rather than guessing).
    """
    parts = urlsplit(server_url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    host = parts.netloc.rsplit("@", 1)[-1]
    return f"{parts.scheme}://{host}"


def _handshake_authorized(
    websocket: WebSocket,
    *,
    token: str,
    allowed_origins: frozenset[str],
) -> bool:
    """Validate a direct-listener WebSocket handshake before accepting.

    :param websocket: The not-yet-accepted WebSocket.
    :param token: The listener's bearer token.
    :param allowed_origins: Origins allowed for browser handshakes.
    :returns: ``True`` when the handshake may proceed.
    """
    supplied = websocket.query_params.get("token", "")
    try:
        token_ok = bool(supplied) and secrets.compare_digest(supplied, token)
    except TypeError:
        # Non-ASCII garbage in the query param; definitionally wrong.
        token_ok = False
    if not token_ok:
        return False
    origin = websocket.headers.get("origin")
    if origin is None:
        # Non-browser client (no Origin header); the token is the gate.
        return True
    return origin in allowed_origins


def create_direct_attach_app(
    attach_handler: AttachHandler,
    *,
    token: str,
    allowed_origins: frozenset[str],
) -> FastAPI:
    """Build the minimal ASGI app served on the loopback listener.

    Exposes exactly two WebSocket routes and nothing else — the
    runner's full RPC surface stays tunnel-only:

    - ``/probe``: validates the handshake, accepts, closes. Lets the
      browser test reachability without touching any terminal (a real
      attach forks a tmux client and seeds a capture, so probing the
      attach route itself would cost real work per page load).
    - ``/v1/sessions/{sid}/resources/terminals/{tid}/attach``: the same
      path shape and handler as the tunnel-dispatched attach route, so
      the browser-facing wire protocol is identical on both paths.

    :param attach_handler: The runner app's attach coroutine
        (``app.state.terminal_attach_handler``).
    :param token: Bearer token required on every handshake.
    :param allowed_origins: Browser origins allowed to connect.
    :returns: The FastAPI app for :func:`start_direct_attach_listener`.
    """
    app = FastAPI(openapi_url=None, docs_url=None, redoc_url=None)

    def _authorized(websocket: WebSocket) -> bool:
        return _handshake_authorized(
            websocket,
            token=token,
            allowed_origins=allowed_origins,
        )

    @app.websocket("/probe")
    async def probe(websocket: WebSocket) -> None:
        """Accept and immediately close so a client can test reachability."""
        if not _authorized(websocket):
            # Close before accept rejects the handshake (HTTP 403).
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await websocket.accept()
        await websocket.close()

    @app.websocket("/v1/sessions/{session_id}/resources/terminals/{terminal_id}/attach")
    async def direct_terminal_attach(
        websocket: WebSocket,
        session_id: str,
        terminal_id: str,
        read_only: bool = Query(default=False),
        transport: str | None = Query(default=None),
    ) -> None:
        """Token-gated wrapper around the runner's attach handler."""
        if not _authorized(websocket):
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
        await attach_handler(
            websocket,
            session_id,
            terminal_id,
            read_only=read_only,
            transport=transport,
        )

    return app


async def _await_quietly(task: asyncio.Task[None]) -> None:
    """Await a finished or cancelled listener task without ever raising.

    The listener is best-effort, so a server that died on its own is
    logged rather than propagated into the runner's startup or shutdown.
    ``asyncio.wait`` never re-raises the task's own failure, so the
    outcome is read back off the task instead of caught.

    :param task: The listener task, already exiting or cancelled.
    """
    await asyncio.wait({task}, timeout=_SHUTDOWN_TIMEOUT_S)
    if not task.done() or task.cancelled():
        return
    if (exc := task.exception()) is not None:
        _logger.warning(
            "direct-attach listener exited with an error",
            exc_info=exc,
            extra={"session_id": runner_primary_session_id()},
        )


class DirectAttachListener:
    """A running loopback listener: the uvicorn server, its task, and port."""

    def __init__(self, server: uvicorn.Server, task: asyncio.Task[None], port: int) -> None:
        self._server = server
        self._task = task
        self.port = port

    async def stop(self) -> None:
        """Signal the server to exit and await it briefly (best-effort)."""
        self._server.should_exit = True
        # asyncio.wait never re-raises the task's own failure, so a listener
        # that already died cannot break the caller's shutdown path.
        await asyncio.wait({self._task}, timeout=_SHUTDOWN_TIMEOUT_S)
        if not self._task.done():
            self._task.cancel()
        await _await_quietly(self._task)


async def start_direct_attach_listener(app: FastAPI) -> DirectAttachListener | None:
    """Serve *app* on an ephemeral 127.0.0.1 port.

    :param app: The app from :func:`create_direct_attach_app`.
    :returns: The running listener, or ``None`` when startup failed —
        callers continue relay-only; the listener is an optimization
        and must never block or break the runner.
    """
    config = uvicorn.Config(
        app,
        host=DIRECT_ATTACH_BIND_HOST,
        port=0,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve(), name="runner-direct-attach")
    deadline = asyncio.get_running_loop().time() + _STARTUP_TIMEOUT_S
    while not server.started:
        if task.done() or asyncio.get_running_loop().time() >= deadline:
            _logger.warning(
                "direct-attach listener failed to start; relay-only",
                extra={"session_id": runner_primary_session_id()},
            )
            task.cancel()
            await _await_quietly(task)
            return None
        await asyncio.sleep(_STARTUP_POLL_S)
    try:
        sockets = server.servers[0].sockets
        port = int(sockets[0].getsockname()[1])
    except (IndexError, AttributeError, OSError):
        _logger.warning(
            "direct-attach listener has no bound socket; relay-only",
            extra={"session_id": runner_primary_session_id()},
        )
        await DirectAttachListener(server, task, 0).stop()
        return None
    _logger.info(
        "direct-attach listener on %s:%d",
        DIRECT_ATTACH_BIND_HOST,
        port,
        extra={"session_id": runner_primary_session_id()},
    )
    return DirectAttachListener(server, task, port)
