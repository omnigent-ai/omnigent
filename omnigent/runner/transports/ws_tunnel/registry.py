"""Server-side registry of live runner WebSocket tunnels (Phase 4).

Per ``designs/RUNNER.md`` §2 "Server-side registry" + §3 "Adapters
on each side", the server keeps an in-memory registry mapping
``runner_id`` → live WebSocket. The :class:`WSTunnelTransport`
looks up the ``WebSocket`` for a given runner, sends a frame, and
reads response frames back via per-`req_id` reassembly queues
managed here.

This module ships:
- :class:`RunnerSession`: per-runner state (live WS, advertised
  capabilities, last-frame-received time, in-flight req_ids).
- :class:`TunnelRegistry`: thread-safe (asyncio-safe) map of
  runner_id → RunnerSession + per-(runner_id, req_id) reassembly
  queues.
- :class:`WebSocketLike`: minimal protocol that real WebSockets and
  test fakes both satisfy. Lets the transport-level code be
  unit-tested without a real network.

Lifecycle: :meth:`register` is called when a fresh tunnel opens;
:meth:`deregister` is called on tunnel close. While registered, the
server can :meth:`route_response_frame` to drop an incoming
``response.*`` frame into the right reassembly queue, and the
transport calls :meth:`open_request` / :meth:`close_request` to
manage per-request lifecycle.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import concurrent.futures
import contextlib
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from omnigent.runner.transports.ws_tunnel.frames import (
    Frame,
    HelloFrame,
    ResponseBodyFrame,
    ResponseEndFrame,
    ResponseHeadFrame,
    WSCloseFrame,
    WSFrame,
)

_logger = logging.getLogger(__name__)


class WebSocketLike(Protocol):
    """Minimal WebSocket protocol used by the registry + transport.

    Both starlette's ``WebSocket`` and our test fakes implement it.
    Async-send-text + async-receive-text is the entire surface — we
    intentionally don't deal with binary frames or close codes here
    because v1's frame protocol is text-only JSON.
    """

    async def send_text(self, data: str) -> None: ...

    async def receive_text(self) -> str: ...


@dataclass
class RunnerSession:
    """Per-runner state living in the registry while the tunnel is open.

    :param runner_id: The runner's UUID (advertised on hello).
    :param ws: The live WebSocket — used by the transport to send
        ``request`` frames and by the route handler that owns the
        WS to receive incoming ``response.*`` frames.
    :param hello: The hello frame the runner sent on connect.
        Capabilities are read from here for routing decisions.
    :param loop: Event loop that owns the Starlette WebSocket.
    :param outbound_queue: Queue consumed by the WebSocket route's
        sender task. Request-side code may run on another loop, so it
        enqueues writes here instead of calling ``ws.send_text``
        directly.
    :param connected_at: Unix epoch float of connect time.
    :param last_frame_at: Unix epoch float of the most recent frame
        from this runner. Updated on every receive — feeds the
        watchdog inactivity check.
    :param owner: Authenticated user who established the tunnel,
        e.g. ``"alice@example.com"``. ``None`` when auth is
        disabled (single-user mode). Used to enforce runner
        ownership: only the owner (or an admin) may bind sessions
        to this runner.
    :param in_flight: Per-req_id reassembly state. Each entry holds
        a head Future + body queue + end Event so the transport can
        await heads, iterate body chunks, and detect end.
    """

    runner_id: str
    ws: WebSocketLike
    hello: HelloFrame
    loop: asyncio.AbstractEventLoop
    outbound_queue: asyncio.Queue[str | None]
    connected_at: float
    last_frame_at: float
    owner: str | None
    in_flight: dict[str, RequestState] = field(default_factory=dict)
    # Per-channel state for tunneled WebSocket attaches.  Keys are
    # 8-char hex channel ids; values hold the inbound queue consumed
    # by whichever side terminated the attach.
    ws_channels: dict[str, WSChannelState] = field(default_factory=dict)


@dataclass
class RequestState:
    """Per-(runner_id, req_id) reassembly state.

    The transport awaits ``head_future`` for the response head, then
    drains ``body_queue`` for body chunks, and ``end_event`` signals
    end-of-response (or tunnel-failure: ``aborted_with`` carries an
    exception that the transport re-raises).

    :param loop: Event loop that owns the future and queue. Response
        frames arrive on the server's WebSocket route loop, while
        consumers often wait from a different async loop in another
        thread, so all wakeups must be scheduled onto this loop.
    :param session: Runner session that owns this request. Stored so
        cleanup and cancel frames target the same generation even if a
        newer tunnel registers with the same runner id.
    :param head_future: Future resolved with the response head.
    :param body_queue: Queue of response body frames plus a ``None``
        sentinel at end of stream.
    :param end_event: Event set when the response end frame arrives.
    :param aborted_with: Error to raise from the body iterator after
        a tunnel disconnect or registry abort.
    """

    loop: asyncio.AbstractEventLoop
    session: RunnerSession
    head_future: asyncio.Future[ResponseHeadFrame]
    body_queue: asyncio.Queue[ResponseBodyFrame | None]
    end_event: asyncio.Event = field(default_factory=asyncio.Event)
    aborted_with: BaseException | None = None


# Inbound channel-queue item shape:
#   ("data", bytes)         — runner ASGI sent a binary frame
#   ("text", str)           — runner ASGI sent a text frame
#   ("close", (code, reason)) — peer closed the channel
#   None                    — local abort sentinel; the channel's
#                             consumer should raise ConnectionClosed.
WSInboundItem = tuple[str, object] | None


@dataclass
class WSChannelState:
    """Per-(runner_id, ch_id) state for a tunneled WS attach.

    The consumer side of the tunnel (server for browser attach,
    runner for the ASGI dispatch) pops items off ``inbound_queue``;
    the receive loop on its tunnel side pushes them.

    :param loop: Event loop that owns ``inbound_queue``. Frames may
        arrive on a different loop and must be scheduled across.
    :param session: Session that owns the channel — stored so
        cleanup can race a session replacement without leaking onto
        the new generation.
    :param inbound_queue: Channel inbound items; see
        :data:`WSInboundItem`.
    """

    loop: asyncio.AbstractEventLoop
    session: RunnerSession
    inbound_queue: asyncio.Queue[WSInboundItem] = field(default_factory=asyncio.Queue)


@dataclass
class RunnerConnectWaitState:
    """
    In-memory wait state for requests waiting on one runner to connect.

    The state exists only while at least one active request is waiting.
    It is removed when the runner registers, when the final waiter times
    out, or when the final waiter is cancelled.

    :param started_at: Unix epoch float when the first waiter for this
        runner id was registered.
    :param waiters: Futures to resolve when the runner registers. Each
        future belongs to the event loop of the request that is waiting.
    """

    started_at: float
    waiters: set[asyncio.Future[RunnerSession]] = field(default_factory=set)


class TunnelRegistry:
    """In-memory map of runner_id → :class:`RunnerSession`.

    All registry mutations are protected by a thread lock because
    the WebSocket route loop and consumer loops can access the
    same session concurrently. Async waiters are still woken on their
    owning loop via ``call_soon_threadsafe``.

    The registry is rebuilt from scratch on server reboot — runners
    reconnect on backoff and re-register via the WS endpoint that
    owns the registry's lifecycle.
    """

    def __init__(
        self,
        *,
        max_connect_waiters_per_runner: int = 1024,
        max_connect_waiters_total: int = 8192,
    ) -> None:
        """
        Create an empty runner tunnel registry.

        :param max_connect_waiters_per_runner: Maximum active
            ``wait_for_runner`` futures allowed for one runner id, e.g.
            ``1024``. Additional callers do not get registered as
            event-driven waiters; they wait for their timeout and do one
            final registry check instead, so the waiter map cannot grow
            without bound under a burst.
        :param max_connect_waiters_total: Maximum active
            ``wait_for_runner`` futures allowed across all runner ids,
            e.g. ``8192``. Additional callers use the same bounded
            overflow path as the per-runner cap.
        :raises ValueError: If either waiter limit is less than one.
        """
        if max_connect_waiters_per_runner < 1:
            raise ValueError("max_connect_waiters_per_runner must be at least 1")
        if max_connect_waiters_total < 1:
            raise ValueError("max_connect_waiters_total must be at least 1")
        self._sessions: dict[str, RunnerSession] = {}
        self._connect_waits: dict[str, RunnerConnectWaitState] = {}
        self._max_connect_waiters_per_runner = max_connect_waiters_per_runner
        self._max_connect_waiters_total = max_connect_waiters_total
        self._connect_waiter_total = 0
        self._lock = threading.RLock()

    # ── Session lifecycle ────────────────────────────────

    def register(
        self,
        runner_id: str,
        ws: WebSocketLike,
        hello: HelloFrame,
        *,
        owner: str | None = None,
    ) -> RunnerSession:
        """Add a new session.

        Per RUNNER.md §2 "Newest wins": if a session already exists
        for the same runner_id (from a previous tunnel that lagged
        on cleanup), the OLD one is discarded and the new one
        replaces it. Any in-flight requests on the old session are
        aborted with a ConnectionError so awaiters get a clean
        failure rather than hanging.

        :param runner_id: Runner id to register.
        :param ws: Live WebSocket connection.
        :param hello: Hello frame the runner sent on connect.
        :param owner: Authenticated user who established the tunnel,
            e.g. ``"alice@example.com"``. ``None`` when auth is
            disabled.
        :returns: The newly created :class:`RunnerSession`.
        """
        loop = asyncio.get_running_loop()
        now = time.time()
        session = RunnerSession(
            runner_id=runner_id,
            ws=ws,
            hello=hello,
            loop=loop,
            outbound_queue=asyncio.Queue(),
            connected_at=now,
            last_frame_at=now,
            owner=owner,
        )
        with self._lock:
            old = self._sessions.pop(runner_id, None)
            if old is not None:
                self._abort_session_inflight(
                    old,
                    ConnectionError(
                        "tunnel replaced by newer connection (newest-wins, RUNNER.md §2)"
                    ),
                )
            self._sessions[runner_id] = session
            wait_state = self._connect_waits.pop(runner_id, None)
            if wait_state is not None:
                self._connect_waiter_total -= len(wait_state.waiters)
        if old is not None:
            _retire_session_writer(old, code=4000, reason="tunnel replaced")
        if wait_state is not None:
            for waiter in list(wait_state.waiters):
                _resolve_connect_waiter(waiter, session)
        return session

    def deregister(
        self,
        runner_id: str,
        session: RunnerSession | None = None,
    ) -> RunnerSession | None:
        """Remove a session and abort all its in-flight requests.

        Called by the WS route handler when the tunnel closes for
        any reason (clean shutdown, network error, etc.). The abort
        ensures awaiters of in-flight requests don't hang — they
        get a ConnectionError and propagate it up.

        :param runner_id: Runner id to remove, e.g.
            ``"runner_0123456789abcdef"``.
        :param session: Optional generation guard. When provided,
            deregistration only removes the registry entry if the
            current entry is this exact session object. This prevents
            stale route handlers from deleting a newer tunnel.
        :returns: The removed session, or ``None`` when the runner is
            already offline or the guard did not match.
        """
        with self._lock:
            current = self._sessions.get(runner_id)
            if current is None or (session is not None and current is not session):
                return None
            removed = self._sessions.pop(runner_id)
            in_flight_count = len(removed.in_flight)
            if in_flight_count:
                _logger.warning(
                    "Deregistering runner %s; aborting %d in-flight request(s)",
                    runner_id,
                    in_flight_count,
                )
            else:
                _logger.info("Deregistering runner %s; no in-flight requests", runner_id)
            self._abort_session_inflight(
                removed,
                ConnectionError("tunnel closed before request completed"),
            )
        _retire_session_writer(removed, code=4003, reason="tunnel closed")
        return removed

    @staticmethod
    def _abort_session_inflight(session: RunnerSession, error: BaseException) -> None:
        for state in list(session.in_flight.values()):
            _call_soon_threadsafe(state, lambda state=state: _abort_request_state(state, error))
        session.in_flight.clear()
        for channel in list(session.ws_channels.values()):
            _call_channel_soon_threadsafe(
                channel, lambda ch=channel: ch.inbound_queue.put_nowait(None)
            )
        session.ws_channels.clear()

    def get(self, runner_id: str) -> RunnerSession | None:
        """Return the session for a runner_id, or None if not online."""
        with self._lock:
            return self._sessions.get(runner_id)

    def is_runner_telemetry_opted_out(self, runner_id: str) -> bool:
        """Return whether the runner's host has opted out of telemetry.

        :param runner_id: Runner id, e.g. ``"runner_0123456789abcdef"``.
        :returns: ``True`` when the runner sent ``telemetry_opt_out=True``
            in its hello frame, or when the runner is offline (unknown
            runners default to not opted out).
        """
        session = self.get(runner_id)
        if session is None:
            return False
        return session.hello.telemetry_opt_out

    async def wait_for_runner(
        self,
        runner_id: str,
        *,
        timeout_s: float,
    ) -> RunnerSession | None:
        """
        Wait until a runner registers or the timeout expires.

        This is the event-driven counterpart to repeatedly calling
        :meth:`get`. The waiter state is bounded and transient: each
        waiter is removed on timeout/cancellation, and ``register``
        removes the whole state for the runner id before resolving the
        waiters.

        :param runner_id: Runner id to wait for, e.g.
            ``"runner_0123456789abcdef"``.
        :param timeout_s: Maximum seconds to wait, e.g. ``3.0``.
        :returns: The registered :class:`RunnerSession`, or ``None`` if
            the runner did not connect before the timeout.
        """
        if timeout_s <= 0:
            return self.get(runner_id)

        loop = asyncio.get_running_loop()
        future: asyncio.Future[RunnerSession] = loop.create_future()
        overflow_reason: str | None = None
        with self._lock:
            current = self._sessions.get(runner_id)
            if current is not None:
                return current
            state = self._connect_waits.get(runner_id)
            if state is not None and len(state.waiters) >= self._max_connect_waiters_per_runner:
                overflow_reason = "per-runner"
            elif self._connect_waiter_total >= self._max_connect_waiters_total:
                overflow_reason = "global"
            else:
                if state is None:
                    state = RunnerConnectWaitState(started_at=time.time())
                    self._connect_waits[runner_id] = state
                state.waiters.add(future)
                self._connect_waiter_total += 1

        if overflow_reason is not None:
            _logger.warning(
                "%s connect waiter cap reached for runner %s; waiting %.1fs without "
                "registering another waiter",
                overflow_reason,
                runner_id,
                timeout_s,
            )
            await asyncio.sleep(timeout_s)
            return self.get(runner_id)

        try:
            return await asyncio.wait_for(future, timeout=timeout_s)
        except asyncio.TimeoutError:
            return None
        finally:
            with self._lock:
                state = self._connect_waits.get(runner_id)
                if state is not None and future in state.waiters:
                    state.waiters.remove(future)
                    self._connect_waiter_total -= 1
                    if not state.waiters:
                        self._connect_waits.pop(runner_id, None)

    def connect_waiter_count(self, runner_id: str | None = None) -> int:
        """
        Return the number of active runner-connect waiters.

        Intended for tests and diagnostics only.

        :param runner_id: Optional runner id to inspect, e.g.
            ``"runner_0123456789abcdef"``. When omitted, returns the
            total waiter count across all runner ids.
        :returns: Count of active waiters.
        """
        with self._lock:
            if runner_id is not None:
                state = self._connect_waits.get(runner_id)
                return 0 if state is None else len(state.waiters)
            return self._connect_waiter_total

    def connect_wait_started_at(self, runner_id: str) -> float | None:
        """
        Return when the current wait state for a runner id was created.

        Intended for tests and diagnostics only.

        :param runner_id: Runner id to inspect, e.g.
            ``"runner_0123456789abcdef"``.
        :returns: Unix epoch float for the first active waiter, or
            ``None`` when no request is currently waiting.
        """
        with self._lock:
            state = self._connect_waits.get(runner_id)
            return None if state is None else state.started_at

    def online_runner_ids(self) -> list[str]:
        """Stable-ordered list of currently-online runner_ids.

        Order is dict-insertion-order (Python 3.7+ guarantee), which
        gives the routing layer a deterministic round-robin without
        extra bookkeeping.
        """
        with self._lock:
            return list(self._sessions.keys())

    def runner_owner(self, runner_id: str) -> str | None:
        """Return the owner of a registered runner, or ``None``.

        :param runner_id: Runner id to look up, e.g.
            ``"runner_0123456789abcdef"``.
        :returns: Owner user id, or ``None`` when the runner is
            offline or was registered without an owner.
        """
        with self._lock:
            session = self._sessions.get(runner_id)
            if session is None:
                return None
            return session.owner

    def mark_frame_seen(self, session: RunnerSession) -> bool:
        """Record that a frame arrived for ``session``.

        :param session: Session that received the frame.
        :returns: ``True`` if the session is still current,
            ``False`` if it has been replaced or deregistered.
        """
        with self._lock:
            if self._sessions.get(session.runner_id) is not session:
                return False
            session.last_frame_at = time.time()
            return True

    def seconds_since_last_frame(self, session: RunnerSession) -> float | None:
        """Return idle seconds for the current session generation.

        :param session: Session to inspect.
        :returns: Seconds since the last received frame, or ``None``
            when ``session`` is stale.
        """
        with self._lock:
            if self._sessions.get(session.runner_id) is not session:
                return None
            return time.time() - session.last_frame_at

    # ── Per-request lifecycle ────────────────────────────

    def open_request(self, runner_id: str, req_id: str) -> RequestState:
        """Allocate reassembly state for a new outgoing request.

        :raises KeyError: If the runner isn't online.
        :raises ValueError: If a request with this ``req_id`` is
            already in flight on this runner. req_ids must be unique
            per session.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            session = self._sessions.get(runner_id)
            if session is None:
                raise KeyError(runner_id)
            if req_id in session.in_flight:
                raise ValueError(f"req_id {req_id!r} already in flight on runner {runner_id!r}")
            state = RequestState(
                loop=loop,
                session=session,
                head_future=loop.create_future(),
                body_queue=asyncio.Queue(),
            )
            session.in_flight[req_id] = state
            return state

    def close_request(
        self,
        runner_id: str,
        req_id: str,
        session: RunnerSession | None = None,
    ) -> None:
        """Drop reassembly state for a completed (or aborted) request.

        :param runner_id: Runner id that owns the request, e.g.
            ``"runner_0123456789abcdef"``.
        :param req_id: Tunnel request id, e.g.
            ``"7a0f7f7cb90f4a5fb5a8071fd0b77568"``.
        :param session: Optional session object that owns the
            request. Allows stale-session cleanup after newest-wins
            replacement has removed the session from the registry.
        :returns: None.
        """
        with self._lock:
            target = session or self._sessions.get(runner_id)
            if target is None:
                return
            target.in_flight.pop(req_id, None)

    def request_is_open(self, session: RunnerSession, req_id: str) -> bool:
        """Return whether a request is still in flight on ``session``.

        :param session: Session that owns the request.
        :param req_id: Tunnel request id, e.g.
            ``"7a0f7f7cb90f4a5fb5a8071fd0b77568"``.
        :returns: ``True`` if the request is still open.
        """
        with self._lock:
            return req_id in session.in_flight

    # ── WS channel lifecycle ─────────────────────────────

    def open_ws_channel(
        self,
        runner_id: str,
        ch_id: str,
        *,
        session: RunnerSession | None = None,
    ) -> WSChannelState:
        """Allocate a per-channel state for a tunneled WS attach.

        :param runner_id: Runner that owns the channel.
        :param ch_id: New channel id, e.g. ``"a1b2c3d4"``.
        :param session: Optional generation guard. When provided,
            allocation only succeeds if the registry's current
            session for ``runner_id`` is this object — guards
            against the runner reconnecting between callsite
            decisions.
        :returns: A fresh :class:`WSChannelState` registered on the
            session.
        :raises KeyError: If the runner is offline or ``session``
            is stale.
        :raises ValueError: If ``ch_id`` is already allocated.
        """
        loop = asyncio.get_running_loop()
        with self._lock:
            current = self._sessions.get(runner_id)
            if current is None or (session is not None and current is not session):
                raise KeyError(runner_id)
            if ch_id in current.ws_channels:
                raise ValueError(f"ws ch_id {ch_id!r} already open on runner {runner_id!r}")
            state = WSChannelState(loop=loop, session=current)
            current.ws_channels[ch_id] = state
            return state

    def close_ws_channel(
        self,
        runner_id: str,
        ch_id: str,
        session: RunnerSession | None = None,
    ) -> None:
        """Drop a channel from the registry.

        Idempotent: closing an unknown channel is a no-op so both
        sides can safely call this on teardown without racing.
        """
        with self._lock:
            target = session or self._sessions.get(runner_id)
            if target is None:
                return
            target.ws_channels.pop(ch_id, None)

    def route_ws_inbound(
        self,
        runner_id: str,
        frame: Frame,
        session: RunnerSession | None = None,
    ) -> bool:
        """Push a WS data/close frame onto its channel inbound queue.

        :returns: ``True`` if the frame matched a known channel and
            was delivered; ``False`` for orphans (logged by caller).
        """
        with self._lock:
            current = self._sessions.get(runner_id)
            if current is None or (session is not None and current is not session):
                return False
            current.last_frame_at = time.time()
            if not isinstance(frame, (WSFrame, WSCloseFrame)):
                return False
            channel = current.ws_channels.get(frame.ch_id)
            if channel is None:
                return False

        item: WSInboundItem
        if isinstance(frame, WSCloseFrame):
            item = ("close", (frame.code, frame.reason))
        else:
            if frame.encoding == "utf-8":
                item = ("text", frame.data)
            elif frame.encoding == "base64":
                try:
                    decoded = base64.b64decode(frame.data, validate=True)
                except (binascii.Error, ValueError):
                    _logger.warning(
                        "ws-channel %s: dropping frame with malformed base64",
                        frame.ch_id,
                    )
                    return False
                item = ("data", decoded)
            else:
                _logger.warning(
                    "ws-channel %s: dropping frame with unknown encoding %r",
                    frame.ch_id,
                    frame.encoding,
                )
                return False

        return _call_channel_soon_threadsafe(
            channel, lambda: channel.inbound_queue.put_nowait(item)
        )

    async def send_text(self, session: RunnerSession, data: str) -> None:
        """Enqueue one outbound WebSocket frame on the session's owner loop.

        :param session: Current session generation that should send
            the frame.
        :param data: Encoded tunnel frame JSON.
        :returns: None after the frame has been accepted into the
            route-loop outbound queue.
        :raises ConnectionError: If ``session`` is no longer the
            registry's current generation for its runner id.
        """
        ack: concurrent.futures.Future[None] = concurrent.futures.Future()

        def _enqueue() -> None:
            """Run on ``session.loop`` and enqueue the outbound frame."""
            error: ConnectionError | None = None
            with self._lock:
                if self._sessions.get(session.runner_id) is not session:
                    error = ConnectionError(f"runner {session.runner_id!r} tunnel was replaced")
                else:
                    session.outbound_queue.put_nowait(data)
            if error is not None:
                if not ack.done():
                    ack.set_exception(error)
            else:
                if not ack.done():
                    ack.set_result(None)

        _call_session_soon_threadsafe(session, _enqueue)
        await asyncio.wrap_future(ack)

    # ── Routing incoming frames ──────────────────────────

    def route_response_frame(
        self,
        runner_id: str,
        frame: Frame,
        session: RunnerSession | None = None,
    ) -> bool:
        """Route an incoming response frame to the right reassembly queue.

        :param runner_id: Runner id that sent the frame, e.g.
            ``"runner_0123456789abcdef"``.
        :param frame: Decoded tunnel frame received from the runner.
        :param session: Optional session-generation guard. When set,
            frames from stale route handlers are ignored instead of
            being routed into a newer session for the same runner id.
        :returns: True if the frame's req_id matches a tracked
            in-flight request; False otherwise (orphan frame —
            logged but not raised; could be a late frame for a
            request that was already cancelled).
        """
        with self._lock:
            current = self._sessions.get(runner_id)
            if current is None or (session is not None and current is not session):
                return False
            current.last_frame_at = time.time()
            if not isinstance(frame, ResponseHeadFrame | ResponseBodyFrame | ResponseEndFrame):
                return False
            req_id = frame.id
            state = current.in_flight.get(req_id)
            if state is None:
                return False
        if isinstance(frame, ResponseHeadFrame):
            if _call_soon_threadsafe(state, lambda: _set_response_head(state, frame)):
                return True
            self.close_request(runner_id, req_id, session=current)
            return False
        if isinstance(frame, ResponseBodyFrame):
            if _call_soon_threadsafe(state, lambda: _enqueue_response_body(state, frame)):
                return True
            self.close_request(runner_id, req_id, session=current)
            return False
        if isinstance(frame, ResponseEndFrame):
            if _call_soon_threadsafe(state, lambda: _end_response_body(state)):
                return True
            self.close_request(runner_id, req_id, session=current)
            return False
        return False

    # ── Observability ────────────────────────────────────

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)

    def __contains__(self, runner_id: str) -> bool:
        with self._lock:
            return runner_id in self._sessions


def _retire_session_writer(session: RunnerSession, *, code: int, reason: str) -> None:
    """Stop a session's sender task and best-effort close its socket.

    :param session: Stale or closed session to retire.
    :param code: WebSocket close code, e.g. ``4000``.
    :param reason: WebSocket close reason.
    :returns: None.
    """

    def _retire() -> None:
        """Run on the WebSocket owner loop."""
        session.outbound_queue.put_nowait(None)
        close = getattr(session.ws, "close", None)
        if close is not None:
            with contextlib.suppress(Exception):
                task = asyncio.create_task(close(code=code, reason=reason))
                task.add_done_callback(_discard_task_exception)

    with contextlib.suppress(RuntimeError):
        _call_session_soon_threadsafe(session, _retire)


def _abort_request_state(state: RequestState, error: BaseException) -> None:
    """Abort one request on the loop that owns its waiters.

    :param state: Request state to abort.
    :param error: Exception to surface to the head waiter or body
        iterator, e.g. ``ConnectionError("tunnel closed")``.
    :returns: None.
    """
    state.aborted_with = error
    if not state.head_future.done():
        state.head_future.set_exception(error)
    # Wake the body iterator so it sees aborted_with.
    state.end_event.set()
    # Sentinel-push to unblock any pending get().
    state.body_queue.put_nowait(None)


def _set_response_head(state: RequestState, frame: ResponseHeadFrame) -> None:
    """Resolve a request's response-head future.

    :param state: Request state whose head future should be
        resolved.
    :param frame: Response head frame, e.g. a
        ``ResponseHeadFrame(status=200, ...)``.
    :returns: None.
    """
    if not state.head_future.done():
        state.head_future.set_result(frame)


def _resolve_connect_waiter(
    future: asyncio.Future[RunnerSession],
    session: RunnerSession,
) -> None:
    """
    Resolve a runner-connect waiter on the waiter's owning event loop.

    :param future: Future created by ``wait_for_runner``.
    :param session: Registered runner session to deliver.
    :returns: None.
    """

    def _set_result() -> None:
        """Set the future result if the waiter is still active."""
        if not future.done():
            future.set_result(session)

    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    waiter_loop = future.get_loop()
    if running_loop is waiter_loop:
        _set_result()
        return
    try:
        waiter_loop.call_soon_threadsafe(_set_result)
    except RuntimeError:
        _logger.debug(
            "Dropping runner-connect wakeup for closed waiter loop (runner_id=%s)",
            session.runner_id,
            exc_info=True,
        )


def _enqueue_response_body(state: RequestState, frame: ResponseBodyFrame) -> None:
    """Append one body frame to the request's body queue.

    :param state: Request state whose body queue receives the
        frame.
    :param frame: Response body frame, e.g. a
        ``ResponseBodyFrame(encoding="utf-8", ...)``.
    :returns: None.
    """
    state.body_queue.put_nowait(frame)


def _end_response_body(state: RequestState) -> None:
    """Signal response-body completion.

    :param state: Request state whose body iterator should stop.
    :returns: None.
    """
    state.end_event.set()
    # Push a sentinel so any pending body_queue.get() unblocks.
    state.body_queue.put_nowait(None)


def _call_soon_threadsafe(state: RequestState, callback: Callable[[], None]) -> bool:
    """Run ``callback`` on the event loop that owns ``state``.

    ``TunnelRegistry.route_response_frame`` is called by the
    server's WebSocket route loop, but the corresponding
    :class:`WSTunnelTransport` request can be blocked in a different async
    workflow loop on another thread. Plain ``Future.set_result`` or
    ``Queue.put_nowait`` from the WebSocket thread mutates state but
    does not wake the waiter. Scheduling onto ``state.loop`` makes
    response head/body/end delivery cross-loop safe.

    :param state: Request state whose owning loop should execute
        ``callback``.
    :param callback: Zero-argument callable to run on
        ``state.loop``.
    :returns: ``True`` if the callback ran or was scheduled,
        ``False`` when the request-owner loop was already closed.
    """
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if running_loop is state.loop:
        callback()
        return True
    try:
        state.loop.call_soon_threadsafe(callback)
    except RuntimeError:
        _logger.debug(
            "Dropping tunnel response wakeup for closed request loop (runner_id=%s)",
            state.session.runner_id,
            exc_info=True,
        )
        return False
    return True


def _call_channel_soon_threadsafe(
    state: WSChannelState,
    callback: Callable[[], None],
) -> bool:
    """Run ``callback`` on the loop that owns ``state.inbound_queue``.

    Returns ``True`` when scheduled, ``False`` if the channel's
    owner loop has been closed (e.g. the consumer task was already
    torn down) — caller treats this like an orphaned frame.
    """
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if running_loop is state.loop:
        callback()
        return True
    try:
        state.loop.call_soon_threadsafe(callback)
    except RuntimeError:
        _logger.debug(
            "Dropping ws-channel wakeup for closed loop (runner_id=%s)",
            state.session.runner_id,
            exc_info=True,
        )
        return False
    return True


def _call_session_soon_threadsafe(
    session: RunnerSession,
    callback: Callable[[], None],
) -> None:
    """Run ``callback`` on the event loop that owns ``session``.

    :param session: Session whose WebSocket owner loop should run
        ``callback``.
    :param callback: Zero-argument callable to run on
        ``session.loop``.
    :returns: None.
    """
    try:
        running_loop = asyncio.get_running_loop()
    except RuntimeError:
        running_loop = None
    if running_loop is session.loop:
        callback()
        return
    session.loop.call_soon_threadsafe(callback)


def _discard_task_exception(task: asyncio.Task[object]) -> None:
    """Consume a best-effort close task's exception.

    :param task: Completed close task.
    :returns: None.
    """
    if task.cancelled():
        return
    with contextlib.suppress(Exception):
        task.exception()
