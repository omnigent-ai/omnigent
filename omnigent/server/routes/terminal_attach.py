"""WebSocket endpoint exposing an agent's live terminals to the browser.

This module hosts the resource-addressed terminal-attach route:

- ``WS /sessions/{session_id}/resources/terminals/{terminal_id}/attach``
  - open a bidirectional bridge between a browser xterm.js client
  and the runner-side tmux session named by ``terminal_id``.

Runner-aware execution
----------------------

Terminal state lives in whichever process owns the
:class:`TerminalRegistry` that ``sys_terminal_launch`` mutated:

- **In-process runner**: the server passes
  :func:`omnigent.runtime.get_terminal_registry` into
  :func:`omnigent.runner.create_runner_app`, so the registry is
  shared. The server resolves the terminal id locally and bridges
  the PTY in-process.
- **Out-of-process runner** over the WebSocket tunnel: the runner
  owns the tmux socket. The server proxies WebSocket frames over
  the tunnel via a multiplexed WS channel
  (``omnigent/server/_runner_ws_tunnel.py``), and the runner's
  resource-addressed WS route runs ``tmux attach`` and bridges the
  PTY.

The proxy uses a factory configured via
:func:`omnigent.runtime.set_runner_ws_factory` in the server
lifespan. When the factory is unset, the server route falls back
to resolving the terminal in the local registry.

Wire protocol on the WebSocket
------------------------------

- **Server → client**: every PTY read is forwarded as a *binary*
  WebSocket frame. xterm.js's ``term.write()`` accepts ``Uint8Array``
  directly and runs it through its ANSI parser, so colors, cursor
  motion, alternate screen, mouse modes all work transparently.
- **Client → server**:
    - **Text frames** are JSON control messages:
      ``{"type": "resize", "cols": N, "rows": M}``. Parsed and applied
      to the PTY via ``ioctl(TIOCSWINSZ)``. Unknown shapes are ignored
      so future control-message additions don't immediately break
      older servers.
    - **Binary frames** are raw input bytes written verbatim to the
      PTY. xterm.js's ``onData`` callback emits these for keystrokes,
      pasted text, and mouse-mode reports.

Read-only mode
--------------

When the URL has ``?read_only=true``, binary input frames are dropped
silently at the server *and* the runner. The attach process itself
runs ``tmux attach -r`` as defense-in-depth, even if a frame got
past the application filter, tmux would refuse keystrokes from this
client.

Write attach is owner-only
--------------------------

A terminal is a single shared PTY driving one process that runs as the
session owner. Raw keystroke bytes carry no per-user identity, so input
typed by anyone other than the owner would be acted on — and, for the
agent's TUI, persisted into conversation history — as if the owner typed
it. To keep that attribution honest, an *interactive* (write) attach
requires ``LEVEL_OWNER``; non-owners can only attach read-only and drive
the agent through the chat composer, which carries the real sender's
identity. This holds uniformly for the agent's own terminal and for
user-launched shells, since both attach through this route.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Final

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect, WebSocketException
from starlette import status

from omnigent.errors import OmnigentError
from omnigent.runtime import (
    get_runner_ws_factory,
    get_terminal_registry,
)
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import require_access
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.terminals.control_bridge import bridge_tmux_control_to_websocket
from omnigent.terminals.ws_bridge import (
    WS_CLOSE_INTERNAL_ERROR,
    WS_CLOSE_TERMINAL_NOT_FOUND,
    bridge_tmux_pty_to_websocket,
)

_logger = logging.getLogger(__name__)

_WS_CLOSE_TERMINAL_NOT_FOUND: Final[int] = WS_CLOSE_TERMINAL_NOT_FOUND
_WS_CLOSE_INTERNAL_ERROR: Final[int] = WS_CLOSE_INTERNAL_ERROR


def create_terminal_attach_router(
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    conversation_store: ConversationStore | None = None,
) -> APIRouter:
    """
    Build the router exposing the terminal-attach WebSocket route.

    Wired into the FastAPI app under the ``/v1`` prefix in
    :func:`omnigent.server.app.create_app`. The list/CRUD endpoints
    for terminals live on the sessions router under
    ``/v1/sessions/{id}/resources/terminals``; this module only owns
    the WebSocket bridge.

    :param auth_provider: Optional provider used to authenticate the
        WebSocket handshake. When ``None`` and permissions are disabled,
        attach behaves as before for single-user/local deployments.
    :param permission_store: Optional session permission store. When
        provided, a read-only attach requires read access and an
        interactive (write) attach requires owner access — only the
        session owner may type into the shared PTY.
    :param conversation_store: Conversation store used by permission checks.
    :returns: An :class:`APIRouter` carrying the attach route.
    """
    router = APIRouter()

    @router.websocket("/sessions/{session_id}/resources/terminals/{terminal_id}/attach")
    async def attach_terminal_by_resource_id(
        websocket: WebSocket,
        session_id: str,
        terminal_id: str,
        read_only: bool = Query(default=False),
        transport: str | None = Query(default=None),
    ) -> None:
        """
        Attach to a terminal by resource id via WebSocket.

        Proxies to the runner's resource-addressed WS endpoint when a
        runner tunnel factory is installed, or falls back to resolving
        the terminal id locally and bridging in-process.

        :param websocket: The accepted FastAPI :class:`WebSocket`.
        :param session_id: Session/conversation identifier.
        :param terminal_id: Opaque terminal resource id,
            e.g. ``"terminal_bash_s1"``.
        :param read_only: Pass ``-r`` to tmux when ``True``.
        :param transport: Optional per-attach transport override
            (``"control"`` / ``"pty"``); forwarded to the runner and used to
            pick the control-mode vs PTY bridge in-process. ``None`` defers to
            the terminal spec / global default.
        """
        from omnigent.entities.session_resources import (
            resolve_terminal_entry_by_resource_id,
        )

        await _authorize_terminal_attach(
            websocket,
            session_id=session_id,
            read_only=read_only,
            auth_provider=auth_provider,
            permission_store=permission_store,
            conversation_store=conversation_store,
        )
        await websocket.accept()

        ws_factory = get_runner_ws_factory()
        if ws_factory is not None:
            from urllib.parse import urlencode

            qs_params = {"read_only": "true" if read_only else "false"}
            if transport is not None:
                qs_params["transport"] = transport
            qs = urlencode(qs_params)
            runner_path = (
                f"/v1/sessions/{session_id}/resources/terminals/{terminal_id}/attach?{qs}"
            )
            try:
                runner_cm = ws_factory(runner_path)
            except Exception:  # noqa: BLE001
                await websocket.close(
                    code=_WS_CLOSE_INTERNAL_ERROR,
                    reason="runner attach factory failed",
                )
                return
            try:
                from omnigent.runtime import telemetry

                with telemetry.span(
                    "terminal.attach",
                    attributes={
                        "session.id": session_id,
                        "terminal.id": terminal_id,
                        "terminal.read_only": read_only,
                        "terminal.mode": "runner-proxy",
                    },
                ):
                    async with runner_cm as runner_ws:
                        await _shuttle_ws_frames(websocket, runner_ws)
            except _RunnerWSClosed as closed:
                code = (
                    closed.code
                    if closed.code and closed.code >= 1000
                    else _WS_CLOSE_INTERNAL_ERROR
                )
                with contextlib.suppress(RuntimeError):
                    await websocket.close(
                        code=code,
                        reason=closed.reason or "",
                    )
            except Exception:  # noqa: BLE001
                with contextlib.suppress(RuntimeError):
                    await websocket.close(
                        code=_WS_CLOSE_INTERNAL_ERROR,
                        reason="runner attach proxy failed",
                    )
            return

        try:
            terminal_registry = get_terminal_registry()
        except RuntimeError:
            await websocket.close(
                code=_WS_CLOSE_TERMINAL_NOT_FOUND,
                reason="terminal registry not available",
            )
            return

        entry = resolve_terminal_entry_by_resource_id(
            session_id,
            terminal_id,
            terminal_registry,
        )
        if entry is None or not entry.instance.running or not await entry.instance.is_alive():
            await websocket.close(
                code=_WS_CLOSE_TERMINAL_NOT_FOUND,
                reason="terminal resource not found or not running",
            )
            return

        from omnigent.inner.terminal import (
            TERMINAL_TRANSPORT_CONTROL,
            resolve_terminal_transport,
        )
        from omnigent.runtime import telemetry

        resolved_transport = resolve_terminal_transport(
            override=transport,
            spec_transport=entry.instance.terminal_transport,
        )
        with telemetry.span(
            "terminal.attach",
            attributes={
                "session.id": session_id,
                "terminal.id": terminal_id,
                "terminal.read_only": read_only,
                "terminal.mode": "in-process",
                "terminal.transport": resolved_transport,
            },
        ):
            bridge = (
                bridge_tmux_control_to_websocket
                if resolved_transport == TERMINAL_TRANSPORT_CONTROL
                else bridge_tmux_pty_to_websocket
            )
            await bridge(
                websocket,
                socket_path=str(entry.instance.socket_path),
                tmux_target=entry.instance.tmux_target,
                read_only=read_only,
            )

    return router


async def _authorize_terminal_attach(
    websocket: WebSocket,
    *,
    session_id: str,
    read_only: bool,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
    conversation_store: ConversationStore | None,
) -> None:
    """
    Authorize a terminal-attach WebSocket before accepting it.

    Interactive attaches write bytes to the shared PTY, which runs as the
    session owner and (for the agent's TUI) persists input into history
    under the owner's identity. Raw keystrokes carry no per-user
    attribution, so an interactive attach requires ``LEVEL_OWNER`` — only
    the owner can drive the terminal. Read-only attaches still expose
    terminal output, so read access is the minimum; non-owners attach
    read-only and send input through the chat composer (which carries the
    real sender's identity). When permissions are disabled
    (``permission_store is None``), preserve the existing
    single-user/dev behavior.

    :param websocket: The incoming FastAPI :class:`WebSocket`, used to
        resolve the caller's identity via *auth_provider*.
    :param session_id: Session/conversation identifier the attach
        targets, e.g. ``"conv_abc123"``.
    :param read_only: ``True`` for a view-only attach (requires
        ``LEVEL_READ``); ``False`` for an interactive write attach
        (requires ``LEVEL_OWNER``).
    :param auth_provider: Provider used to resolve the caller's user id
        from the WebSocket handshake. Required when *permission_store*
        is set.
    :param permission_store: Session permission store. ``None`` disables
        all checks (single-user/dev mode).
    :param conversation_store: Conversation store consulted by the
        access check. Required when *permission_store* is set.
    :raises WebSocketException: With ``WS_1008_POLICY_VIOLATION`` when
        auth is misconfigured, the caller is unauthenticated, or the
        caller lacks the required level for the requested mode.
    """
    if permission_store is None:
        return
    if auth_provider is None or conversation_store is None:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="terminal attach authorization is not configured",
        )

    user_id = auth_provider.get_user_id(websocket)
    if user_id is None:
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="authentication required",
        )

    required_level = LEVEL_READ if read_only else LEVEL_OWNER
    try:
        await require_access(
            user_id,
            session_id,
            required_level,
            permission_store,
            conversation_store,
        )
    except OmnigentError as exc:
        _logger.info(
            "Rejected terminal attach for session %s as user %s: %s",
            session_id,
            user_id,
            exc,
        )
        raise WebSocketException(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="not authorized",
        ) from exc


class _RunnerWSClosed(Exception):
    """Carries a runner-side close so the browser side mirrors it."""

    def __init__(self, code: int | None, reason: str | None) -> None:
        super().__init__(f"runner WS closed: code={code} reason={reason!r}")
        self.code = code
        self.reason = reason


async def _shuttle_ws_frames(browser_ws: WebSocket, runner_ws: object) -> None:
    """
    Forward frames between *browser_ws* (FastAPI) and *runner_ws*
    (websockets client connection) until either side closes.

    The two libraries use slightly different APIs:

    - FastAPI: ``await ws.receive()`` returns a dict with ``text``
      or ``bytes``. Sends are :meth:`WebSocket.send_text` and
      :meth:`WebSocket.send_bytes`.
    - :mod:`websockets`: ``await ws.recv()`` returns ``str`` or
      ``bytes``; ``await ws.send(data)`` accepts either.

    We translate at the boundary. When either side raises, we
    cancel the other task and let the outer ``finally`` close the
    browser-side WS.

    :param browser_ws: The browser-facing FastAPI WebSocket.
    :param runner_ws: The runner-facing websockets client.
    """
    from websockets.exceptions import ConnectionClosed

    async def _browser_to_runner() -> None:
        try:
            while True:
                msg = await browser_ws.receive()
                msg_type = msg.get("type")
                if msg_type == "websocket.disconnect":
                    return
                text = msg.get("text")
                data = msg.get("bytes")
                if text is not None:
                    await runner_ws.send(text)  # type: ignore[attr-defined]
                elif data is not None:
                    await runner_ws.send(data)  # type: ignore[attr-defined]
        except WebSocketDisconnect:
            return
        except ConnectionClosed:
            return

    async def _runner_to_browser() -> None:
        try:
            while True:
                msg = await runner_ws.recv()  # type: ignore[attr-defined]
                if isinstance(msg, bytes | bytearray | memoryview):
                    await browser_ws.send_bytes(bytes(msg))
                else:
                    await browser_ws.send_text(msg)
        except ConnectionClosed as cc:
            # Surface the runner-side close code so the browser
            # mirrors it (4404 for missing terminal, etc.). Use
            # ``rcvd`` rather than the deprecated ``code`` / ``reason``
            # attributes so we read what the peer actually sent
            # (``sent`` would be the close we transmitted).
            rcvd = cc.rcvd
            code = rcvd.code if rcvd is not None else None
            reason = rcvd.reason if rcvd is not None else None
            raise _RunnerWSClosed(code, reason) from cc

    b2r = asyncio.create_task(_browser_to_runner())
    r2b = asyncio.create_task(_runner_to_browser())

    runner_closed: _RunnerWSClosed | None = None
    try:
        done, pending = await asyncio.wait(
            {b2r, r2b},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        for task in done:
            exc = task.exception()
            if isinstance(exc, _RunnerWSClosed):
                runner_closed = exc
            elif exc is not None:
                _logger.warning("terminal-attach proxy: task crashed: %r", exc)
    finally:
        if runner_closed is not None:
            raise runner_closed
