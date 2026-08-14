"""Tunnel-backed ``ws_factory`` for browser terminal attach.

The server's :mod:`omnigent.server.routes.terminal_attach` route
proxies browser xterm.js WebSocket connections to whichever runner
owns the conversation's tmux session via a
``set_runner_ws_factory(...)``-installed callable. Previously the
runner exposed a separate WebSocket listener that the server dialed
directly. Now the only server↔runner transport is the
runner's outbound WS tunnel, so this module bridges that gap by
multiplexing tunneled WS *channels* onto the same tunnel using the
``ws.open`` / ``ws.frame`` / ``ws.close`` frame kinds.

Wire flow per browser attach:

1. Browser opens ``WS /v1/sessions/{id}/resources/terminals/
   {terminal_id}/attach``.
2. Terminal-attach route resolves the conversation's pinned runner,
   calls the factory with the runner-side path it constructs.
3. The factory returns :class:`_TunneledWSConn`. Entering its async
   context allocates a fresh ``ch_id``, registers a
   :class:`WSChannelState` on the tunnel registry, and sends a
   ``ws.open`` frame down the tunnel naming the runner-side path.
4. The runner's ASGI dispatch invokes its
   ``@app.websocket("/v1/sessions/{id}/resources/terminals/
   {terminal_id}/attach")`` route, which runs
   ``bridge_tmux_pty_to_websocket`` unchanged.
5. The terminal-attach route's existing shuttle pumps frames in
   both directions through ``conn.send()`` / ``conn.recv()``.
6. Either side's close emits a ``ws.close`` frame; the receiver
   surfaces it as a :class:`websockets.exceptions.ConnectionClosed`
   so the shuttle's existing error-translation branch handles it.
"""

from __future__ import annotations

import base64
import contextlib
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import TYPE_CHECKING

from websockets.exceptions import ConnectionClosed
from websockets.frames import Close

from omnigent.errors import OmnigentError
from omnigent.runner.transports.ws_tunnel.frames import (
    WSCloseFrame,
    WSFrame,
    WSOpenFrame,
    encode_frame,
)

if TYPE_CHECKING:
    from omnigent.runner.routing import RunnerRouter
    from omnigent.runner.transports.ws_tunnel.registry import (
        RunnerSession,
        TunnelRegistry,
        WSChannelState,
    )

_logger = logging.getLogger(__name__)


class WrongReplicaWSError(RuntimeError):
    """Raised by the tunnel factory on a wrong-replica routing miss.

    Carries the ``WRONG_REPLICA`` signal (the runner tunnel is bound but
    lives on a different replica) out to the terminal-attach route, which
    maps it to WS close code ``4400`` so the client re-dials WITHOUT the key.
    A plain :class:`RuntimeError` would collapse to the generic ``4500``
    internal-error close, which the client treats as a dead-end. Subclasses
    ``RuntimeError`` so existing ``except Exception`` fallbacks that close
    ``4500`` still catch it.
    """


# Match the runner-side WS attach path that
# ``attach_terminal_by_resource_id`` constructs:
#   ``/v1/sessions/{conv}/resources/terminals/{terminal_id}/attach``
#
# ``session_id`` and ``conversation_id`` are the same identifier by
# construction, so the captured value feeds
# ``router.client_for_existing_conversation()``.
_RUNNER_PATH_RE = re.compile(r"^/v1/sessions/(?P<conv>[^/?]+)/resources/terminals/[^/?]+/attach")


@dataclass(frozen=True)
class DirectAttachEndpoint:
    """A runner's advertised loopback terminal-attach listener.

    :param port: Loopback TCP port on the runner's machine,
        e.g. ``52341``.
    :param token: Per-runner-process bearer token the listener
        requires on every handshake.
    """

    port: int
    token: str


def make_direct_attach_resolver(
    router: RunnerRouter,
    registry: TunnelRegistry,
) -> Callable[[str], DirectAttachEndpoint | None]:
    """Build a resolver from conversation id to its runner's direct-attach advert.

    Mirrors :func:`make_tunnel_ws_factory`'s routing (pinned runner →
    live tunnel session) but only reads the hello advert; installed via
    :func:`omnigent.runtime.set_runner_direct_attach_resolver` so the
    terminals API can hand same-machine browsers a relay-free attach URL.

    :param router: Routes conversation ids to pinned runner ids.
    :param registry: Tunnel registry that owns the live runner sessions.
    :returns: Callable returning the advert, or ``None`` when the
        conversation has no pinned runner, the runner is offline, or it
        advertised no listener.
    """

    def resolver(conversation_id: str) -> DirectAttachEndpoint | None:
        try:
            routed = router.client_for_existing_conversation(conversation_id)
        except OmnigentError:
            return None
        if routed is None:
            return None
        session = registry.get(routed.runner_id)
        if session is None:
            return None
        port = session.hello.direct_attach_port
        token = session.hello.direct_attach_token
        if port is None or not token:
            return None
        return DirectAttachEndpoint(port=port, token=token)

    return resolver


def make_tunnel_ws_factory(
    router: RunnerRouter,
    registry: TunnelRegistry,
) -> Callable[[str], _TunneledWSConn]:
    """Build a ``ws_factory`` callable that opens tunneled WS channels.

    Install via :func:`omnigent.runtime.set_runner_ws_factory`. The
    factory shape (callable ``(runner_path) -> async-context``) is
    what :mod:`omnigent.server.routes.terminal_attach` already
    expects.

    :param router: Routes conversation ids to pinned runner ids.
    :param registry: Tunnel registry that owns the live runner WS
        sessions.
    :returns: A callable that the terminal-attach route will use.
    """

    def factory(runner_path: str) -> _TunneledWSConn:
        match = _RUNNER_PATH_RE.match(runner_path)
        if match is None:
            raise ValueError(f"unrecognized runner_path: {runner_path!r}")
        conversation_id = match.group("conv")
        try:
            routed = router.client_for_existing_conversation(conversation_id)
        except OmnigentError as exc:
            raise RuntimeError(str(exc)) from exc
        if routed is None:
            raise RuntimeError(f"no runner pinned for conversation {conversation_id!r}")
        session = registry.get(routed.runner_id)
        if session is None:
            raise RuntimeError(
                f"runner {routed.runner_id!r} is offline for conversation {conversation_id!r}"
            )
        return _TunneledWSConn(
            registry=registry,
            session=session,
            runner_path=runner_path,
        )

    return factory


class _TunneledWSConn:
    """WS-client-shaped wrapper around one tunnel WS channel.

    Implements the minimum contract that
    :func:`omnigent.server.routes.terminal_attach._shuttle_ws_frames`
    expects of its ``runner_ws`` argument: ``async with``,
    ``send(data)``, ``recv()``, and a
    :class:`websockets.exceptions.ConnectionClosed` raise on
    runner-side close (with ``.rcvd`` carrying peer code+reason).
    """

    def __init__(
        self,
        *,
        registry: TunnelRegistry,
        session: RunnerSession,
        runner_path: str,
    ) -> None:
        self._registry = registry
        self._session = session
        self._runner_path = runner_path
        self._ch_id: str = ""
        self._state: WSChannelState | None = None
        self._closed_locally = False

    async def __aenter__(self) -> _TunneledWSConn:
        # 4 random bytes → 8 hex chars; plenty of entropy for a
        # ch_id only required to be unique within one runner session.
        self._ch_id = secrets.token_hex(4)
        # Pass the captured session as a generation guard so the
        # channel is only allocated when the captured session is
        # still the registry's current session for this runner.
        try:
            self._state = self._registry.open_ws_channel(
                self._session.runner_id,
                self._ch_id,
                session=self._session,
            )
        except KeyError as exc:
            raise ConnectionError(
                f"runner {self._session.runner_id!r} tunnel was replaced "
                f"before WS channel could be opened"
            ) from exc
        path, _, qs = self._runner_path.partition("?")
        await self._registry.send_text(
            self._session,
            encode_frame(WSOpenFrame(ch_id=self._ch_id, path=path, query_string=qs)),
        )
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # Best-effort ws.close so the runner tears down its tmux
        # attach cleanly. Swallow errors — the tunnel may already
        # be gone, in which case the runner-side dispatch task
        # already saw the cancel.
        if not self._closed_locally:
            self._closed_locally = True
            with contextlib.suppress(Exception):
                await self._registry.send_text(
                    self._session,
                    encode_frame(WSCloseFrame(ch_id=self._ch_id, code=1000, reason="")),
                )
        self._registry.close_ws_channel(
            self._session.runner_id, self._ch_id, session=self._session
        )

    async def send(self, data: str | bytes) -> None:
        """Forward a browser-side frame to the runner over the tunnel.

        :param data: ``str`` → ``ws.frame`` with utf-8 encoding (the
            JSON resize control frames). ``bytes`` → ``ws.frame``
            with base64 encoding (keystrokes / mouse events).
        """
        if self._closed_locally:
            return
        if isinstance(data, str):
            frame = WSFrame(ch_id=self._ch_id, data=data, encoding="utf-8")
        else:
            frame = WSFrame(
                ch_id=self._ch_id,
                data=base64.b64encode(bytes(data)).decode("ascii"),
                encoding="base64",
            )
        await self._registry.send_text(self._session, encode_frame(frame))

    async def recv(self) -> str | bytes:
        """Pop the next runner-side payload off the channel queue.

        :returns: ``bytes`` for PTY output, ``str`` for any text
            payload the runner emits.
        :raises ConnectionClosed: On runner-side close — including
            a synthesized 1006-style close when the underlying
            tunnel itself aborts.
        """
        assert self._state is not None, "recv() called before __aenter__"
        item = await self._state.inbound_queue.get()
        if item is None:
            raise _connection_closed(1006, "tunnel aborted")
        tag, payload = item
        if tag == "data":
            assert isinstance(payload, (bytes, bytearray))
            return bytes(payload)
        if tag == "text":
            assert isinstance(payload, str)
            return payload
        if tag == "close":
            assert isinstance(payload, tuple)
            code, reason = payload
            raise _connection_closed(code, reason)
        raise RuntimeError(f"ws-channel {self._ch_id!r}: unknown inbound tag {tag!r}")


def _connection_closed(code: int, reason: str) -> ConnectionClosed:
    """Build a :class:`ConnectionClosed` with ``.rcvd = Close(code, reason)``.

    The terminal-attach shuttle reads ``cc.rcvd.code`` / ``.reason``
    when surfacing the runner-side close to the browser.
    """
    return ConnectionClosed(rcvd=Close(code, reason), sent=None)
