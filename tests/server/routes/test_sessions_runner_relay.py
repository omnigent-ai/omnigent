"""Tests for AP's runner stream relay startup handshake."""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections.abc import AsyncIterator
from types import SimpleNamespace, TracebackType
from typing import Any, cast

import httpx
import pytest

from omnigent.runner.transports.ws_tunnel.frames import (
    HelloFrame,
    ResponseBodyFrame,
    ResponseHeadFrame,
)
from omnigent.runner.transports.ws_tunnel.registry import TunnelRegistry
from omnigent.runner.transports.ws_tunnel.transport import WSTunnelTransport
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.server.helpers import start_session_stream_collector


class _HeartbeatStreamResponse:
    """
    Async context manager that mimics ``httpx.AsyncClient.stream``.

    :param release: Event that lets the fake stream finish after the
        ready heartbeat has been consumed.
    """

    def __init__(self, release: asyncio.Event) -> None:
        """
        Initialize the fake streaming response.

        :param release: Event used to unblock the stream tail.
        """
        self._release = release

    async def __aenter__(self) -> _HeartbeatStreamResponse:
        """
        Enter the async stream context.

        :returns: This fake response.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """
        Exit the async stream context.

        :param exc_type: Exception type, if the stream exited with an
            exception.
        :param exc: Exception instance, if any.
        :param traceback: Exception traceback, if any.
        :returns: None.
        """
        del exc_type, exc, traceback

    async def aiter_text(self) -> AsyncIterator[str]:
        """
        Yield a ready heartbeat, then finish after release.

        :yields: SSE text chunks in the same data-line shape the runner
            emits over HTTP.
        """
        yield 'data: {"type": "session.heartbeat"}\n\n'
        await self._release.wait()
        yield "data: [DONE]\n\n"


class _HeartbeatRunnerClient:
    """
    Fake runner client whose stream emits a ready heartbeat.

    :param release: Event that lets the fake response finish.
    """

    def __init__(self, release: asyncio.Event) -> None:
        """
        Initialize the fake runner client.

        :param release: Event used to unblock the stream tail.
        """
        self._release = release
        self.stream_calls: list[tuple[str, str, Any]] = []

    def stream(
        self,
        method: str,
        path: str,
        *,
        timeout: Any,
    ) -> _HeartbeatStreamResponse:
        """
        Return the scripted streaming response.

        :param method: HTTP method, e.g. ``"GET"``.
        :param path: Request path, e.g.
            ``"/v1/sessions/4e92b5a0c0ee6db3f874f9c4a3f855a5/stream"``.
        :param timeout: Timeout object passed by the relay.
        :returns: Fake streaming response.
        """
        self.stream_calls.append((method, path, timeout))
        return _HeartbeatStreamResponse(self._release)


class _NoBannerStreamResponse:
    """Fake replacement stream that exits without becoming ready."""

    def __init__(self, release: asyncio.Event) -> None:
        self._release = release

    async def __aenter__(self) -> _NoBannerStreamResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def aiter_text(self) -> AsyncIterator[str]:
        await self._release.wait()
        yield "data: [DONE]\n\n"


class _DropAfterBannerStreamResponse:
    """Fake initial stream that becomes ready and then disconnects."""

    def __init__(self, drop: asyncio.Event) -> None:
        self._drop = drop

    async def __aenter__(self) -> _DropAfterBannerStreamResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def aiter_text(self) -> AsyncIterator[str]:
        yield 'data: {"type": "session.heartbeat"}\n\n'
        await self._drop.wait()
        raise ConnectionError("replacement needed")


class _DropThenNoBannerRunnerClient:
    """Fake runner whose replacement stream never signals ready."""

    def __init__(
        self,
        drop: asyncio.Event,
        replacement_started: asyncio.Event,
        replacement_release: asyncio.Event,
    ) -> None:
        self._drop = drop
        self._replacement_started = replacement_started
        self._replacement_release = replacement_release
        self._calls = 0

    def stream(
        self,
        method: str,
        path: str,
        *,
        timeout: Any,
    ) -> _DropAfterBannerStreamResponse | _NoBannerStreamResponse:
        del method, path, timeout
        self._calls += 1
        if self._calls == 1:
            return _DropAfterBannerStreamResponse(self._drop)
        self._replacement_started.set()
        return _NoBannerStreamResponse(self._replacement_release)


@pytest.mark.asyncio
async def test_runner_relay_ready_waits_for_runner_heartbeat() -> None:
    """
    Omnigent relay readiness is set only after the runner stream heartbeat.

    Production breakage this catches: accepting a user message after
    merely scheduling the relay task, before Omnigent has actually subscribed
    to runner output. A fast harness can otherwise complete before the
    relay is listening, producing a successful CLI run with empty
    stdout.
    """
    from omnigent.server.routes import sessions as sessions_module

    sessions_module._runner_relay_tasks.clear()
    release = asyncio.Event()
    fake_runner = _HeartbeatRunnerClient(release)

    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            "a7f039e9f1311474878eb7d4699c1013",
            "runner_ready",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=None,
        )

        assert handle is not None
        assert handle.ready.is_set()
        assert fake_runner.stream_calls[0][0] == "GET"
        assert (
            fake_runner.stream_calls[0][1]
            == "/v1/sessions/a7f039e9f1311474878eb7d4699c1013/stream"
        )
    finally:
        release.set()
        handle = sessions_module._runner_relay_tasks.get("a7f039e9f1311474878eb7d4699c1013")
        if handle is not None:
            await asyncio.wait_for(handle.task, timeout=1.0)
        sessions_module._runner_relay_tasks.clear()


class _ScriptedStreamResponse:
    """
    Async context manager mimicking ``httpx.AsyncClient.stream``.

    Emits the ready heartbeat, waits for the test's release gate, then
    replays a scripted turn (events as already-encoded SSE data lines)
    and closes with ``[DONE]``.

    :param release: Event the test sets once its stream collector is
        subscribed, so every scripted event fans out to it.
    :param events: SSE event payload dicts to emit after release, in
        order, e.g. ``[{"type": "response.in_progress", ...}]``.
    """

    def __init__(self, release: asyncio.Event, events: list[dict[str, Any]]) -> None:
        """
        Initialize the scripted streaming response.

        :param release: Event used to gate the scripted turn.
        :param events: Event payload dicts to emit after release.
        """
        self._release = release
        self._events = events

    async def __aenter__(self) -> _ScriptedStreamResponse:
        """
        Enter the async stream context.

        :returns: This fake response.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """
        Exit the async stream context.

        :param exc_type: Exception type, if the stream exited with an
            exception.
        :param exc: Exception instance, if any.
        :param traceback: Exception traceback, if any.
        :returns: None.
        """
        del exc_type, exc, traceback

    async def aiter_text(self) -> AsyncIterator[str]:
        """
        Yield the heartbeat, the gated scripted turn, then ``[DONE]``.

        :yields: SSE text chunks in the same data-line shape the runner
            emits over HTTP.
        """
        yield 'data: {"type": "session.heartbeat"}\n\n'
        await self._release.wait()
        for event in self._events:
            yield f"data: {json.dumps(event)}\n\n"
        yield "data: [DONE]\n\n"


class _ScriptedRunnerClient:
    """
    Fake runner client whose stream replays a scripted turn.

    :param release: Event that gates the scripted turn (set by the
        test once its collector is subscribed).
    :param events: SSE event payload dicts to emit after release.
    """

    def __init__(self, release: asyncio.Event, events: list[dict[str, Any]]) -> None:
        """
        Initialize the fake runner client.

        :param release: Event used to gate the scripted turn.
        :param events: Event payload dicts to emit after release.
        """
        self._release = release
        self._events = events

    def stream(
        self,
        method: str,
        path: str,
        *,
        timeout: Any,
    ) -> _ScriptedStreamResponse:
        """
        Return the scripted streaming response.

        :param method: HTTP method, e.g. ``"GET"``.
        :param path: Request path, e.g.
            ``"/v1/sessions/4e92b5a0c0ee6db3f874f9c4a3f855a5/stream"``.
        :param timeout: Timeout object passed by the relay.
        :returns: Fake streaming response.
        """
        del method, path, timeout
        return _ScriptedStreamResponse(self._release, self._events)


@pytest.mark.asyncio
async def test_relay_text_flush_publishes_persisted_item(db_uri: str) -> None:
    """
    The relay's text flush publishes the persisted message to live clients.

    Scaffold harnesses stream assistant text only as id-less
    ``output_text.delta`` events; the relay buffers and persists the text
    on the terminal event. The flush must then publish a
    ``response.output_item.done`` carrying the store-assigned item id —
    ordered BEFORE the terminal ``response.completed`` — so live clients
    can stamp the id onto the already-rendered streamed block.

    Production breakage this catches: reverting ``_flush_relay_text`` to
    persist-only. The rendered block then stays id-less for the rest of
    the page lifetime, and the web client's itemId-keyed reconnect
    reconciliation splices the persisted copy in next to it as a
    duplicate bubble (the fork-to-relay-agent duplicate-response bug).
    """
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    sessions_module._runner_relay_tasks.clear()
    store = SqlAlchemyConversationStore(db_uri)
    # agent_id=None: the relay never reads the agent row, and a real id
    # would need an agents-table row to satisfy the FK.
    conv = store.create_conversation()
    session_id = conv.id

    response_id = "resp_relay_flush_1"
    turn_events: list[dict[str, Any]] = [
        {
            "type": "response.in_progress",
            "response": {"id": response_id, "model": "debby"},
        },
        # Scaffold-style deltas: no message_id, so no per-message
        # output_item.done ever arrives from the runner itself.
        {"type": "response.output_text.delta", "delta": "Hello "},
        {"type": "response.output_text.delta", "delta": "world."},
        # No usage field: keeps the terminal event off the
        # cost-accumulation path, which this test doesn't exercise.
        {
            "type": "response.completed",
            "response": {"id": response_id, "model": "debby"},
        },
    ]
    release = asyncio.Event()
    fake_runner = _ScriptedRunnerClient(release, turn_events)

    collector = None
    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_relay_flush",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,
        )
        assert handle is not None

        # Subscribe BEFORE releasing the scripted turn so every relay
        # publish deterministically fans out to the collector.
        collector = await start_session_stream_collector(session_id)
        release.set()

        # Drain the live stream up to the terminal event, recording the
        # event-type order. session_stream suppresses nothing here (the
        # session has no native in-flight messages), so the collector
        # sees exactly what a connected web/TUI client would.
        seen_types: list[str] = []
        done_events: list[dict[str, Any]] = []
        while not seen_types or seen_types[-1] != "response.completed":
            event = await collector.next_event()
            seen_types.append(event["type"])
            if event["type"] == "response.output_item.done":
                done_events.append(event)

        # The persisted assistant message reached the store with the
        # full joined delta text. If missing, the flush never persisted.
        items = store.list_items(session_id).data
        messages = [item for item in items if item.type == "message"]
        assert len(messages) == 1, (
            f"Expected exactly one persisted assistant message, got "
            f"{[item.type for item in items]}. Zero means the terminal "
            f"flush didn't persist; more means a segment double-persisted."
        )
        persisted = messages[0]

        # Exactly one output_item.done was published, carrying the
        # store-assigned id and the full text. Zero means the flush is
        # persist-only again (the duplicate-bubble regression); a
        # mismatched id means clients can never reconcile the rendered
        # block against GET /items.
        assert len(done_events) == 1, (
            f"Expected exactly one response.output_item.done on the live "
            f"stream, saw {len(done_events)} in {seen_types}."
        )
        published_item = done_events[0]["item"]
        assert published_item["id"] == persisted.id
        assert published_item["response_id"] == response_id
        assert published_item["role"] == "assistant"
        # Content equality proves the published event carries the same
        # text the deltas streamed — what clients dedupe against.
        assert published_item["content"] == [{"type": "output_text", "text": "Hello world."}]

        # Ordering: the done event must precede response.completed so the
        # client's streamed text section is still open when the id lands
        # (after the terminal event the reducer has closed the block and
        # the id can no longer be stamped onto it).
        assert seen_types.index("response.output_item.done") < seen_types.index(
            "response.completed"
        ), f"output_item.done published after the terminal event: {seen_types}"
    finally:
        release.set()
        if collector is not None:
            await collector.stop()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None:
            await asyncio.wait_for(handle.task, timeout=1.0)
        sessions_module._runner_relay_tasks.clear()
        session_stream.close(session_id)


class _TunnelCloseStreamResponse:
    """
    Async context manager that raises ``ConnectionError`` mid-stream.

    Emits the ready heartbeat, waits for a gate, then raises
    ``ConnectionError`` to simulate a ws-tunnel drop.

    :param gate: Event the test sets once its collector is subscribed,
        so the error fires after the collector can observe it.
    """

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    async def __aenter__(self) -> _TunnelCloseStreamResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def aiter_text(self) -> AsyncIterator[str]:
        yield 'data: {"type": "session.heartbeat"}\n\n'
        await self._gate.wait()
        raise ConnectionError("tunnel closed before request completed")


class _TunnelCloseRunnerClient:
    """Fake runner client whose stream drops with ``ConnectionError``.

    :param gate: Event that gates the error (set by the test once
        its stream collector is subscribed).
    """

    def __init__(self, gate: asyncio.Event) -> None:
        self._gate = gate

    def stream(
        self,
        method: str,
        path: str,
        *,
        timeout: Any,
    ) -> _TunnelCloseStreamResponse:
        del method, path, timeout
        return _TunnelCloseStreamResponse(self._gate)


class _NoopTunnelWS:
    """WebSocket fake for registry registration without I/O."""

    async def send_text(self, data: str) -> None:
        del data

    async def receive_text(self) -> str:
        return await asyncio.Future()


class _SessionStreamTimeoutTransport(WSTunnelTransport):
    """Real tunnel transport whose session stream raises ``ReadTimeout``.

    Keeps the runner registered so attribution must distinguish a live
    tunnel from tunnel loss.
    """

    def __init__(
        self,
        registry: TunnelRegistry,
        runner_id: str,
        gate: asyncio.Event,
    ) -> None:
        super().__init__(registry, runner_id)
        self._gate = gate

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        gate = self._gate

        class _Body(httpx.AsyncByteStream):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield b'data: {"type": "session.heartbeat"}\n\n'
                await gate.wait()
                raise httpx.ReadTimeout(
                    "session stream read timed out",
                    request=request,
                )

        return httpx.Response(
            status_code=200,
            headers={"content-type": "text/event-stream"},
            stream=_Body(),
            request=request,
        )


def _register_test_runner(registry: TunnelRegistry, runner_id: str) -> None:
    """Register ``runner_id`` with a noop websocket for liveness checks."""
    registry.register(
        runner_id,
        _NoopTunnelWS(),
        HelloFrame(runner_version="0.1.0-test", frame_protocol_version=1),
    )


def _registered_tunnel_client(runner_id: str) -> tuple[TunnelRegistry, httpx.AsyncClient]:
    """Build a client over a registered tunnel."""
    registry = TunnelRegistry()
    _register_test_runner(registry, runner_id)
    return registry, httpx.AsyncClient(
        transport=WSTunnelTransport(registry, runner_id),
        base_url="http://runner",
    )


def _stream_timeout_client(
    runner_id: str,
    gate: asyncio.Event,
) -> tuple[TunnelRegistry, httpx.AsyncClient]:
    """Build a live-tunnel client whose session stream raises ``ReadTimeout``."""
    registry = TunnelRegistry()
    _register_test_runner(registry, runner_id)
    client = httpx.AsyncClient(
        transport=_SessionStreamTimeoutTransport(registry, runner_id, gate),
        base_url="http://runner",
    )
    return registry, client


async def _wait_for_new_tunnel_request(
    registry: TunnelRegistry,
    runner_id: str,
    seen: set[str] | None = None,
) -> str:
    """Return the next in-flight request not already in ``seen``."""
    seen = seen if seen is not None else set()
    deadline = time.monotonic() + 4.0
    while time.monotonic() < deadline:
        session = registry.get(runner_id)
        if session is not None:
            for req_id in session.in_flight:
                if req_id not in seen:
                    seen.add(req_id)
                    return req_id
        await asyncio.sleep(0.01)
    raise AssertionError("relay never opened a new tunnel stream request")


def _route_tunnel_sse_start(
    registry: TunnelRegistry,
    runner_id: str,
    req_id: str,
) -> None:
    """Route a successful response head and subscription banner."""
    registry.route_response_frame(
        runner_id,
        ResponseHeadFrame(
            id=req_id,
            status=200,
            headers=[["content-type", "text/event-stream"]],
        ),
    )
    registry.route_response_frame(
        runner_id,
        ResponseBodyFrame(
            id=req_id,
            body='data: {"type": "session.heartbeat"}\n\n',
            encoding="utf-8",
        ),
    )


async def _feed_tunnel_sse_start(
    registry: TunnelRegistry,
    runner_id: str,
) -> None:
    """Wait for an in-flight request and route its response start."""
    req_id = await _wait_for_new_tunnel_request(registry, runner_id)
    _route_tunnel_sse_start(registry, runner_id, req_id)


async def _cleanup_relay_test(
    *,
    session_id: str,
    gate: asyncio.Event | None = None,
    collector: Any | None = None,
    client: httpx.AsyncClient | None = None,
    feeder: asyncio.Task[None] | None = None,
) -> None:
    """Cancel relay leftover state shared by disconnect-attribution tests."""
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    if gate is not None:
        gate.set()
    if feeder is not None:
        feeder.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feeder
    if collector is not None:
        await collector.stop()
    handle = sessions_module._runner_relay_tasks.get(session_id)
    if handle is not None and not handle.task.done():
        handle.task.cancel()
        with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(handle.task, timeout=1.0)
    sessions_module._runner_relay_tasks.clear()
    sessions_module._session_status_cache.pop(session_id, None)
    session_stream.close(session_id)
    if client is not None:
        await client.aclose()


@pytest.mark.asyncio
async def test_relay_publishes_failed_status_on_tunnel_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A tunnel close mid-TURN publishes ``session.status`` "failed".

    Regression test for #1114: before the fix the relay swallowed the
    ``ConnectionError`` and exited silently, leaving the client's SSE
    stream truncated with no error event. The reconnect grace is zeroed
    so the drop is terminal on the first attempt.
    """
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S",
        0.0,
    )
    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    fake_runner = _TunnelCloseRunnerClient(gate)
    session_id = "03048a276e8a91fab748c87a77d638bf"
    # A turn is in flight — the state #1114 is about. Only an interrupted
    # turn is failed by the drop; an idle session stays quiet (see
    # test_relay_stays_quiet_when_runner_leaves_an_idle_session).
    sessions_module._session_status_cache[session_id] = "running"

    collector = None
    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_tunnel_close",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=None,
        )
        assert handle is not None

        # Subscribe BEFORE releasing the error so the published
        # session.status event fans out to the collector.
        collector = await start_session_stream_collector(session_id)
        gate.set()

        # The relay task should finish quickly after the ConnectionError.
        await asyncio.wait_for(handle.task, timeout=2.0)

        # Wait for the failed-status event to arrive at the collector.
        event = await asyncio.wait_for(collector.queue.get(), timeout=2.0)
        assert event.get("type") == "session.status"
        assert event.get("status") == "failed"
        assert event["error"]["code"] == "runner_disconnected"
    finally:
        gate.set()
        if collector is not None:
            await collector.stop()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=1.0)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_relay_publishes_session_stream_lost_when_runner_still_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A session-stream failure while the runner tunnel is up is not
    ``runner_disconnected``.

    Uses a real :class:`WSTunnelTransport` with the runner still
    registered; a read timeout must stamp ``session_stream_lost``. The
    reconnect grace is zeroed so the drop is terminal on the first
    attempt.
    """
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S",
        0.0,
    )
    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    runner_id = "runner_session_stream_lost"
    registry, client = _stream_timeout_client(runner_id, gate)
    session_id = "a1b2c3d4e5f60718293a4b5c6d7e8f90"

    collector = None
    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            runner_id,
            client,
            conversation_store=None,
        )
        assert handle is not None

        collector = await start_session_stream_collector(session_id)
        gate.set()

        await asyncio.wait_for(handle.task, timeout=2.0)

        event = await asyncio.wait_for(collector.queue.get(), timeout=2.0)
        assert event.get("type") == "session.status"
        assert event.get("status") == "failed"
        assert event["error"]["code"] == "session_stream_lost"
        assert registry.get(runner_id) is not None
    finally:
        await _cleanup_relay_test(
            session_id=session_id,
            gate=gate,
            collector=collector,
            client=client,
        )


@pytest.mark.asyncio
async def test_relay_tunnel_replacement_stays_runner_disconnected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Newest-wins replacement aborts must not stamp ``session_stream_lost``.

    Replacement registers the new tunnel before aborting the old stream,
    so a naive "registry still has runner" check would misattribute. The
    recoverable reconnect path only clears ``runner_disconnected``. The
    reconnect grace is zeroed so the drop is terminal on the first
    attempt.
    """
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S",
        0.0,
    )
    sessions_module._runner_relay_tasks.clear()
    runner_id = "runner_tunnel_replacement"
    registry, client = _registered_tunnel_client(runner_id)
    session_id = "c3d4e5f60718293a4b5c6d7e8f90a1b2"

    feeder = asyncio.create_task(_feed_tunnel_sse_start(registry, runner_id))
    collector = None
    try:
        handle = sessions_module._ensure_runner_relay(
            session_id,
            runner_id,
            client,
            conversation_store=None,
        )
        assert handle is not None
        collector = await start_session_stream_collector(session_id)

        await asyncio.wait_for(handle.ready.wait(), timeout=2.0)
        # Newest-wins: register again so the live stream is aborted while
        # a runner remains in the registry.
        _register_test_runner(registry, runner_id)
        await asyncio.wait_for(handle.task, timeout=2.0)

        event = await asyncio.wait_for(collector.queue.get(), timeout=2.0)
        assert event.get("type") == "session.status"
        assert event.get("status") == "failed"
        assert event["error"]["code"] == "runner_disconnected"
        assert registry.get(runner_id) is not None
    finally:
        await _cleanup_relay_test(
            session_id=session_id,
            collector=collector,
            client=client,
            feeder=feeder,
        )


class _NeverReadyStreamResponse:
    """Fake stream that never produces a response banner."""

    async def __aenter__(self) -> _NeverReadyStreamResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def aiter_text(self) -> AsyncIterator[str]:
        await asyncio.Event().wait()
        yield ""


class _DropThenNeverReadyRunnerClient:
    """Fake runner that drops once, then stalls before its banner."""

    def __init__(self) -> None:
        self.calls = 0

    def stream(
        self,
        method: str,
        path: str,
        *,
        timeout: Any,
    ) -> _NeverReadyStreamResponse:
        del method, path, timeout
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("tunnel closed before request completed")
        return _NeverReadyStreamResponse()


class _TwoIncidentStreamResponse:
    """Scripted stream for two separated transport-loss incidents."""

    def __init__(self, client: _TwoIncidentRunnerClient, call: int) -> None:
        self._client = client
        self._call = call

    async def __aenter__(self) -> _TwoIncidentStreamResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def aiter_text(self) -> AsyncIterator[str]:
        yield 'data: {"type": "session.heartbeat"}\n\n'
        if self._call == 1:
            await self._client.first_drop.wait()
            raise ConnectionError("first blip")
        if self._call == 2:
            self._client.second_started.set()
            await self._client.sustained_progress.wait()
            yield 'data: {"type": "session.heartbeat"}\n\n'
            await self._client.second_drop.wait()
            raise self._client.second_error
        self._client.third_started.set()
        await self._client.finish.wait()
        yield "data: [DONE]\n\n"


class _TwoIncidentRunnerClient:
    """Fake runner with two blips separated by sustained health."""

    def __init__(self, *, second_error: Exception | None = None) -> None:
        self.calls = 0
        self.second_error = second_error or ConnectionError("second blip")
        self.first_drop = asyncio.Event()
        self.second_started = asyncio.Event()
        self.sustained_progress = asyncio.Event()
        self.second_drop = asyncio.Event()
        self.third_started = asyncio.Event()
        self.finish = asyncio.Event()

    def stream(
        self,
        method: str,
        path: str,
        *,
        timeout: Any,
    ) -> _TwoIncidentStreamResponse:
        del method, path, timeout
        self.calls += 1
        return _TwoIncidentStreamResponse(self, self.calls)


class _EarlyProgressThenStallResponse:
    """Fake retry that emits early progress and then wedges."""

    def __init__(self, stalled: asyncio.Event) -> None:
        self._stalled = stalled

    async def __aenter__(self) -> _EarlyProgressThenStallResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def aiter_text(self) -> AsyncIterator[str]:
        yield 'data: {"type": "session.heartbeat"}\n\n'
        yield 'data: {"type": "session.status", "status": "running"}\n\n'
        self._stalled.set()
        await asyncio.Event().wait()
        yield ""


class _DropThenEarlyProgressStallRunnerClient:
    """Fake runner that wedges after one early progress event."""

    def __init__(self) -> None:
        self.calls = 0
        self.stalled = asyncio.Event()

    def stream(
        self,
        method: str,
        path: str,
        *,
        timeout: Any,
    ) -> _EarlyProgressThenStallResponse:
        del method, path, timeout
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("initial blip")
        return _EarlyProgressThenStallResponse(self.stalled)


@pytest.mark.asyncio
async def test_relay_retry_attempt_cannot_overrun_reconnect_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A retry stalled before its banner stops at the original deadline."""
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import orchestration

    grace = 0.2
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", grace)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 0.01)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_MAX_INTERVAL_S", 0.01)
    monkeypatch.setattr(orchestration._logger, "warning", lambda *args, **kwargs: None)
    sessions_module._runner_relay_tasks.clear()
    fake_runner = _DropThenNeverReadyRunnerClient()
    store = _RecordingLabelStore(live_status="running")
    session_id = "7a8b9c0d1e2f30415263748596a7b8c9"
    sessions_module._session_status_cache[session_id] = "running"

    await asyncio.to_thread(lambda: None)
    started = time.monotonic()
    try:
        handle = sessions_module._ensure_runner_relay(
            session_id,
            "runner_retry_deadline",
            cast(httpx.AsyncClient, fake_runner),
            conversation_store=cast(ConversationStore, store),
        )
        assert handle is not None
        await asyncio.wait_for(handle.task, timeout=1.0)

        elapsed = time.monotonic() - started
        assert fake_runner.calls >= 2
        assert grace * 0.75 <= elapsed < grace + 1.0
        assert sessions_module._session_status_cache.get(session_id) == "failed"
    finally:
        await _cleanup_relay_test(session_id=session_id)


@pytest.mark.asyncio
async def test_relay_sustained_health_resets_budget_for_later_blip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A second incident after sustained health receives a fresh retry."""
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import orchestration

    grace = 0.1
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", grace)
    monkeypatch.setattr(orchestration, "_RELAY_MAX_DEGRADED_GRACE_WINDOWS", 3.0)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 0.005)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_MAX_INTERVAL_S", 0.005)
    retry_numbers: list[int] = []

    def _record_retry_number(retry_number: int) -> float:
        retry_numbers.append(retry_number)
        return 0.005

    monkeypatch.setattr(orchestration, "_relay_retry_delay", _record_retry_number)
    sessions_module._runner_relay_tasks.clear()
    fake_runner = _TwoIncidentRunnerClient()
    session_id = "9c0d1e2f30415263748596a7b8c9d0e1"

    try:
        handle = sessions_module._ensure_runner_relay(
            session_id,
            "runner_two_incidents",
            cast(httpx.AsyncClient, fake_runner),
            conversation_store=None,
        )
        assert handle is not None
        await asyncio.wait_for(handle.ready.wait(), timeout=1.0)
        fake_runner.first_drop.set()
        await asyncio.wait_for(fake_runner.second_started.wait(), timeout=1.0)

        await asyncio.sleep(grace * 2)
        fake_runner.sustained_progress.set()
        await asyncio.sleep(grace * 2)
        fake_runner.second_drop.set()

        await asyncio.wait_for(fake_runner.third_started.wait(), timeout=2.0)
        fake_runner.finish.set()
        await asyncio.wait_for(handle.task, timeout=2.0)
        assert len(retry_numbers) >= 2
        assert retry_numbers[0] == retry_numbers[-1] == 0
        assert sessions_module._session_status_cache.get(session_id) != "failed"
    finally:
        fake_runner.first_drop.set()
        fake_runner.sustained_progress.set()
        fake_runner.second_drop.set()
        fake_runner.finish.set()
        await _cleanup_relay_test(session_id=session_id)


@pytest.mark.asyncio
async def test_relay_sustained_health_survives_delayed_read_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read timeout cannot erase recovery earned before the stall."""
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import orchestration

    grace = 0.1
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", grace)
    monkeypatch.setattr(orchestration, "_RELAY_MAX_DEGRADED_GRACE_WINDOWS", 3.0)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 0.005)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_MAX_INTERVAL_S", 0.005)
    sessions_module._runner_relay_tasks.clear()
    fake_runner = _TwoIncidentRunnerClient(
        second_error=httpx.ReadTimeout("healthy stream later stalled")
    )
    session_id = "ad1e2f30415263748596a7b8c9d0e1f2"

    try:
        handle = sessions_module._ensure_runner_relay(
            session_id,
            "runner_healthy_then_timeout",
            cast(httpx.AsyncClient, fake_runner),
            conversation_store=None,
        )
        assert handle is not None
        await asyncio.wait_for(handle.ready.wait(), timeout=1.0)
        fake_runner.first_drop.set()
        await asyncio.wait_for(fake_runner.second_started.wait(), timeout=1.0)

        await asyncio.sleep(grace * 2)
        fake_runner.sustained_progress.set()
        await asyncio.sleep(grace * 2)
        fake_runner.second_drop.set()

        await asyncio.wait_for(fake_runner.third_started.wait(), timeout=2.0)
        fake_runner.finish.set()
        await asyncio.wait_for(handle.task, timeout=2.0)
        assert fake_runner.calls >= 3
        assert sessions_module._session_status_cache.get(session_id) != "failed"
    finally:
        fake_runner.first_drop.set()
        fake_runner.sustained_progress.set()
        fake_runner.second_drop.set()
        fake_runner.finish.set()
        await _cleanup_relay_test(session_id=session_id)


@pytest.mark.asyncio
async def test_relay_early_progress_remains_under_degraded_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One early event cannot leave a wedged retry unsupervised."""
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import orchestration

    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 0.05)
    monkeypatch.setattr(orchestration, "_RELAY_MAX_DEGRADED_GRACE_WINDOWS", 3.0)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 0.005)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_MAX_INTERVAL_S", 0.005)
    monkeypatch.setattr(orchestration._logger, "warning", lambda *args, **kwargs: None)
    sessions_module._runner_relay_tasks.clear()
    fake_runner = _DropThenEarlyProgressStallRunnerClient()
    store = _RecordingLabelStore(live_status="running")
    session_id = "0d1e2f30415263748596a7b8c9d0e1f2"
    sessions_module._session_status_cache[session_id] = "running"

    try:
        handle = sessions_module._ensure_runner_relay(
            session_id,
            "runner_early_progress_stall",
            cast(httpx.AsyncClient, fake_runner),
            conversation_store=cast(ConversationStore, store),
        )
        assert handle is not None
        await asyncio.wait_for(fake_runner.stalled.wait(), timeout=1.0)
        await asyncio.wait_for(handle.task, timeout=1.0)
        assert sessions_module._session_status_cache.get(session_id) == "failed"
    finally:
        await _cleanup_relay_test(session_id=session_id)


class _FlappingStreamResponse:
    """Fake stream that becomes ready, makes progress, then drops."""

    async def __aenter__(self) -> _FlappingStreamResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def aiter_text(self) -> AsyncIterator[str]:
        yield 'data: {"type": "session.heartbeat"}\n\n'
        yield 'data: {"type": "session.status", "status": "running"}\n\n'
        raise ConnectionError("flapping tunnel")


class _FlappingRunnerClient:
    """Fake runner whose every stream attempt flaps after one event."""

    def __init__(self) -> None:
        self.calls = 0

    def stream(
        self,
        method: str,
        path: str,
        *,
        timeout: Any,
    ) -> _FlappingStreamResponse:
        del method, path, timeout
        self.calls += 1
        return _FlappingStreamResponse()


@pytest.mark.asyncio
async def test_relay_repeated_flaps_stop_at_absolute_degraded_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeated progress cannot keep a degraded relay alive forever."""
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import orchestration

    grace = 0.08
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", grace)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 0.005)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_MAX_INTERVAL_S", 0.02)
    monkeypatch.setattr(orchestration._logger, "warning", lambda *args, **kwargs: None)
    sessions_module._runner_relay_tasks.clear()
    fake_runner = _FlappingRunnerClient()
    store = _RecordingLabelStore(live_status="running")
    session_id = "8b9c0d1e2f30415263748596a7b8c9d0"

    await asyncio.to_thread(lambda: None)
    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_repeated_flaps",
            cast(httpx.AsyncClient, fake_runner),
            conversation_store=cast(ConversationStore, store),
        )
        assert handle is not None
        await asyncio.wait_for(handle.task, timeout=1.5)

        assert fake_runner.calls > 1
        assert sessions_module._session_status_cache.get(session_id) == "failed"
        assert sessions_module._last_task_error_from_labels(store.labels[session_id]) == {
            "code": "runner_disconnected",
            "message": "Runner disconnected unexpectedly.",
        }
    finally:
        await _cleanup_relay_test(session_id=session_id)


def test_relay_retry_delay_uses_jittered_exponential_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry delay backs off, jitters, and caps at its maximum."""
    from omnigent.server.routes._sessions import orchestration

    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 0.5)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_MAX_INTERVAL_S", 2.0)
    draws = iter((0, 1_000_000, 500_000))
    monkeypatch.setattr(orchestration.secrets, "randbelow", lambda upper: next(draws))

    assert orchestration._relay_retry_delay(0) == pytest.approx(0.4)
    assert orchestration._relay_retry_delay(1) == pytest.approx(1.2)
    assert orchestration._relay_retry_delay(8) == pytest.approx(2.0)


async def _feed_banner_then_stall(registry: TunnelRegistry, runner_id: str) -> None:
    """Serve each new stream request a banner heartbeat, then stall."""
    seen: set[str] = set()
    while True:
        req_id = await _wait_for_new_tunnel_request(registry, runner_id, seen)
        _route_tunnel_sse_start(registry, runner_id, req_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("serve_banner", [False, True], ids=["no-response", "banner-only"])
async def test_relay_zero_progress_stalls_exhaust_nonzero_grace(
    monkeypatch: pytest.MonkeyPatch,
    serve_banner: bool,
) -> None:
    """No response or only the subscription banner must exhaust the grace."""
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import orchestration

    monkeypatch.setattr(orchestration, "_RELAY_STREAM_READ_TIMEOUT_S", 0.3)
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 0.2)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 0.05)
    sessions_module._runner_relay_tasks.clear()
    runner_id = f"runner_zero_progress_{serve_banner}"
    registry, client = _registered_tunnel_client(runner_id)
    session_id = "0718293a4b5c6d7e8f90a1b2c3d4e5f6"

    feeder = (
        asyncio.create_task(_feed_banner_then_stall(registry, runner_id)) if serve_banner else None
    )
    collector = None
    try:
        handle = sessions_module._ensure_runner_relay(
            session_id,
            runner_id,
            client,
            conversation_store=None,
        )
        assert handle is not None
        collector = await start_session_stream_collector(session_id)

        await asyncio.wait_for(handle.task, timeout=3.0)

        event = await asyncio.wait_for(collector.queue.get(), timeout=2.0)
        assert event.get("type") == "session.status"
        assert event.get("status") == "failed"
        assert event["error"]["code"] == "session_stream_lost"
        assert registry.get(runner_id) is not None
    finally:
        await _cleanup_relay_test(
            session_id=session_id,
            collector=collector,
            client=client,
            feeder=feeder,
        )


@pytest.mark.asyncio
async def test_relay_idle_attempt_outliving_grace_refreshes_on_disconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An idle-but-healthy attempt that outlives the grace gets a fresh window.

    Between the banner and the first keepalive heartbeat (10-15s in
    production) a healthy idle stream carries no frames, so a disconnect
    there has ``progress=False``. It must still refresh the grace — the
    attempt provably reconnected and outlived the window. Read-timeout
    stalls stay progress-gated regardless of duration.
    """
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import orchestration

    monkeypatch.setattr(orchestration, "_RELAY_STREAM_READ_TIMEOUT_S", 5.0)
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 0.15)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 0.05)
    sessions_module._runner_relay_tasks.clear()
    runner_id = "runner_idle_disconnect_grace"
    registry, client = _registered_tunnel_client(runner_id)
    session_id = "18293a4b5c6d7e8f90a1b2c3d4e5f607"

    seen: set[str] = set()

    async def _feeder() -> None:
        # Attempt 1: drop before the head so the first failure sets the
        # grace deadline without any banner.
        await _wait_for_new_tunnel_request(registry, runner_id, seen)
        registry.deregister(runner_id)
        _register_test_runner(registry, runner_id)
        # Attempt 2: banner, idle past the whole grace, then disconnect.
        req2 = await _wait_for_new_tunnel_request(registry, runner_id, seen)
        _route_tunnel_sse_start(registry, runner_id, req2)
        await asyncio.sleep(0.45)
        registry.deregister(runner_id)
        _register_test_runner(registry, runner_id)
        # Attempt 3 exists only if attempt 2 refreshed the window: end
        # it cleanly so the relay exits without any failure.
        req3 = await _wait_for_new_tunnel_request(registry, runner_id, seen)
        _route_tunnel_sse_start(registry, runner_id, req3)
        registry.route_response_frame(
            runner_id,
            ResponseBodyFrame(id=req3, body="data: [DONE]\n\n", encoding="utf-8"),
        )

    feeder = asyncio.create_task(_feeder())
    collector = None
    try:
        handle = sessions_module._ensure_runner_relay(
            session_id,
            runner_id,
            client,
            conversation_store=None,
        )
        assert handle is not None
        collector = await start_session_stream_collector(session_id)

        await asyncio.wait_for(handle.task, timeout=3.0)
        await feeder

        # The idle attempt's disconnect must have been retried, not
        # published as a terminal failure.
        while not collector.queue.empty():
            event = collector.queue.get_nowait()
            assert event.get("status") != "failed", event
    finally:
        await _cleanup_relay_test(
            session_id=session_id,
            collector=collector,
            client=client,
            feeder=feeder,
        )


@pytest.mark.asyncio
async def test_relay_slow_banner_then_disconnect_exhausts_nonzero_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attempts whose banner alone outlasts the grace must not refresh it.

    A wedged runner can take longer than the grace to serve the banner
    and then drop immediately. Only the interval after the banner proves
    stream health.
    """
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import orchestration

    monkeypatch.setattr(orchestration, "_RELAY_STREAM_READ_TIMEOUT_S", 5.0)
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 0.2)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 0.05)
    sessions_module._runner_relay_tasks.clear()
    runner_id = "runner_slow_banner_grace"
    registry, client = _registered_tunnel_client(runner_id)
    session_id = "293a4b5c6d7e8f90a1b2c3d4e5f60718"

    seen: set[str] = set()

    async def _feeder() -> None:
        # Every attempt: banner arrives only after the whole grace has
        # elapsed, then the tunnel drops right away.
        while True:
            req_id = await _wait_for_new_tunnel_request(registry, runner_id, seen)
            await asyncio.sleep(0.3)
            _route_tunnel_sse_start(registry, runner_id, req_id)
            # Let the banner chunk reach the relay before dropping.
            await asyncio.sleep(0.02)
            registry.deregister(runner_id)
            _register_test_runner(registry, runner_id)

    feeder = asyncio.create_task(_feeder())
    collector = None
    try:
        handle = sessions_module._ensure_runner_relay(
            session_id,
            runner_id,
            client,
            conversation_store=None,
        )
        assert handle is not None
        collector = await start_session_stream_collector(session_id)

        # Without a post-banner healthy interval no attempt may refresh
        # the grace; the loop must terminate instead of retrying forever.
        await asyncio.wait_for(handle.task, timeout=3.0)

        event = await asyncio.wait_for(collector.queue.get(), timeout=2.0)
        assert event.get("type") == "session.status"
        assert event.get("status") == "failed"
        assert event["error"]["code"] == "session_stream_lost"
    finally:
        await _cleanup_relay_test(
            session_id=session_id,
            collector=collector,
            client=client,
            feeder=feeder,
        )


class _BannerThenReadTimeoutResponse:
    """Fake stream that serves the banner, idles, then times out reading.

    :param idle_s: Seconds to idle after the banner before the read
        timeout fires — set longer than the grace so attempt duration
        alone would look like a healthy idle stream.
    """

    def __init__(self, idle_s: float) -> None:
        self._idle_s = idle_s

    async def __aenter__(self) -> _BannerThenReadTimeoutResponse:
        """Enter the async stream context.

        :returns: This fake response.
        """
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Exit the async stream context.

        :param exc_type: Exception type, if any.
        :param exc: Exception instance, if any.
        :param traceback: Exception traceback, if any.
        :returns: None.
        """
        del exc_type, exc, traceback

    async def aiter_text(self) -> AsyncIterator[str]:
        """Yield the subscription banner, then stall into a read timeout.

        :yields: The runner's subscription-banner SSE frame.
        """
        yield 'data: {"type": "session.heartbeat"}\n\n'
        await asyncio.sleep(self._idle_s)
        raise httpx.ReadTimeout("relay stream read timed out")


class _BannerThenReadTimeoutRunnerClient:
    """Fake runner client (legacy shape) that stalls after every banner.

    Not backed by :class:`WSTunnelTransport`, so a stall here cannot be
    attributed as stream loss (``stream_lost`` stays False) — the timeout
    itself is the only signal that the idle interval wasn't healthy.

    :param idle_s: Seconds each attempt idles after the banner.
    """

    def __init__(self, idle_s: float) -> None:
        self.calls = 0
        self._idle_s = idle_s

    def stream(
        self,
        method: str,
        path: str,
        *,
        timeout: Any,
    ) -> _BannerThenReadTimeoutResponse:
        """Serve another banner-then-stall attempt.

        :param method: Ignored HTTP method.
        :param path: Ignored request path.
        :param timeout: Ignored timeout (the fake raises on its own).
        :returns: The fake streaming response.
        """
        del method, path, timeout
        self.calls += 1
        return _BannerThenReadTimeoutResponse(self._idle_s)


@pytest.mark.asyncio
async def test_relay_read_timeout_without_stream_loss_exhausts_nonzero_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read-timeout stalls exhaust the grace even when unattributable.

    On a transport that exposes no liveness probe (the legacy fixed
    client) a stalled attempt carries ``stream_lost=False``, so only the
    timeout itself distinguishes a wedged stream from a healthy idle one.
    Without that signal each banner-then-stall attempt outlasts the whole
    grace, refreshes the deadline as idle recovery, and terminal failure
    is unreachable. The grace stays non-zero so the terminal state is
    reached by exhausting it, not by skipping it.
    """
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import orchestration

    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 0.2)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 0.05)
    sessions_module._runner_relay_tasks.clear()
    # Each attempt idles past the whole grace before timing out, mirroring
    # production's 45s read timeout vs 10s grace.
    fake_runner = _BannerThenReadTimeoutRunnerClient(idle_s=0.3)
    session_id = "3a4b5c6d7e8f90a1b2c3d4e5f6071829"

    collector = None
    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_banner_then_read_timeout",
            cast(httpx.AsyncClient, fake_runner),
            conversation_store=None,
        )
        assert handle is not None
        collector = await start_session_stream_collector(session_id)

        # Without the timeout signal this retries forever and the wait
        # below times out.
        await asyncio.wait_for(handle.task, timeout=3.0)

        event = await asyncio.wait_for(collector.queue.get(), timeout=2.0)
        assert event.get("type") == "session.status"
        assert event.get("status") == "failed"
        # No liveness probe on this transport, so the stall stays a
        # runner disconnect rather than a stream loss.
        assert event["error"]["code"] == "runner_disconnected"
    finally:
        await _cleanup_relay_test(session_id=session_id, collector=collector)


class _RecordingLabelStore:
    """Minimal conversation store that records ``set_labels`` calls.

    The disconnect path persists the failure cause as durable labels so
    snapshots and child summaries can tell a benign runner disconnect
    from a real task failure (Option B). ``set_labels`` is exercised by
    the tunnel-close path; ``get_conversation`` is read by
    ``_publish_runner_recovered_status`` to gate the clear on the
    persisted disconnect code, so both are implemented here.
    """

    def __init__(self, *, live_status: str = "idle") -> None:
        self.labels: dict[str, dict[str, str]] = {}
        self.live_status = live_status

    def set_labels(self, conversation_id: str, updates: dict[str, str]) -> None:
        self.labels.setdefault(conversation_id, {}).update(updates)

    def get_conversation(self, conversation_id: str) -> Any:
        """Return a conversation-shaped object exposing the read fields.

        ``.labels`` is read by the recovery guard and ``.live_status`` by
        the mid-turn check when the in-memory status cache is cold, so a
        lightweight namespace over both is enough.
        """
        return SimpleNamespace(
            labels=dict(self.labels.get(conversation_id, {})),
            live_status=self.live_status,
        )


@pytest.mark.asyncio
async def test_stream_loss_recovery_waits_for_replacement_relay_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unready replacement keeps the failure for explicit session-init."""
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import orchestration

    sessions_module._runner_relay_tasks.clear()
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 0.2)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_INTERVAL_S", 0.01)
    monkeypatch.setattr(orchestration, "_RELAY_RETRY_MAX_INTERVAL_S", 0.01)
    drop = asyncio.Event()
    replacement_started = asyncio.Event()
    replacement_release = asyncio.Event()
    store = _RecordingLabelStore()
    session_id = "8192a3b4c5d6e7f8091a2b3c4d5e6f70"
    try:
        handle = sessions_module._ensure_runner_relay(
            session_id,
            "runner_replacement_not_ready",
            cast(
                httpx.AsyncClient,
                _DropThenNoBannerRunnerClient(
                    drop,
                    replacement_started,
                    replacement_release,
                ),
            ),
            conversation_store=cast(ConversationStore, store),
        )
        assert handle is not None
        await asyncio.wait_for(handle.ready.wait(), timeout=1.0)
        drop.set()
        await asyncio.wait_for(replacement_started.wait(), timeout=1.0)
        store.labels[session_id] = {
            sessions_module._LAST_TASK_ERROR_CODE_LABEL_KEY: "session_stream_lost",
            sessions_module._LAST_TASK_ERROR_MESSAGE_LABEL_KEY: (
                "Session stream lost unexpectedly."
            ),
        }
        sessions_module._session_status_cache[session_id] = "failed"

        await sessions_module._publish_runner_recovered_status(
            session_id,
            cast(ConversationStore, store),
            require_disconnect_code=True,
        )

        assert handle.ready.is_set() is False
        assert sessions_module._session_status_cache.get(session_id) == "failed"
        assert sessions_module._last_task_error_from_labels(store.labels[session_id]) == {
            "code": "session_stream_lost",
            "message": "Session stream lost unexpectedly.",
        }
    finally:
        drop.set()
        replacement_release.set()
        await _cleanup_relay_test(session_id=session_id)


@pytest.mark.asyncio
async def test_replacing_relay_clears_old_ready_signal() -> None:
    """Replacing a relay invalidates its retained readiness event."""
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions.common import _RelayHandle

    sessions_module._runner_relay_tasks.clear()
    session_id = "b4c5d6e7f8091a2b3c4d5e6f708192a3"
    old_ready = asyncio.Event()
    old_ready.set()
    old_task = asyncio.create_task(asyncio.Event().wait())
    sessions_module._runner_relay_tasks[session_id] = _RelayHandle(
        "runner_old",
        old_task,
        old_ready,
    )
    drop = asyncio.Event()
    replacement_started = asyncio.Event()
    replacement_release = asyncio.Event()

    try:
        handle = sessions_module._ensure_runner_relay(
            session_id,
            "runner_new",
            cast(
                httpx.AsyncClient,
                _DropThenNoBannerRunnerClient(
                    drop,
                    replacement_started,
                    replacement_release,
                ),
            ),
            conversation_store=None,
        )
        assert handle is not None
        assert old_ready.is_set() is False
    finally:
        drop.set()
        replacement_release.set()
        await _cleanup_relay_test(session_id=session_id)
        await asyncio.gather(old_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_lost", "expected_code", "expected_message"),
    [
        pytest.param(
            False,
            "runner_disconnected",
            "Runner disconnected unexpectedly.",
            id="tunnel-close",
        ),
        pytest.param(
            True,
            "session_stream_lost",
            "Session stream lost unexpectedly.",
            id="live-tunnel-stream-loss",
        ),
    ],
)
async def test_relay_persists_transport_error_labels(
    monkeypatch: pytest.MonkeyPatch,
    stream_lost: bool,
    expected_code: str,
    expected_message: str,
) -> None:
    """Tunnel-close and live-stream errors retain their distinct labels."""
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S",
        0.0,
    )
    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    store = _RecordingLabelStore()
    session_id = "82fe36b7ca1bfb567bfbcce4eaa487a1"
    sessions_module._session_status_cache[session_id] = "running"
    client: httpx.AsyncClient | None = None
    if stream_lost:
        runner_id = "runner_stream_loss_labels"
        _registry, client = _stream_timeout_client(runner_id, gate)
        runner_client = client
    else:
        runner_id = "runner_tunnel_close_labels"
        runner_client = cast(httpx.AsyncClient, _TunnelCloseRunnerClient(gate))

    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            runner_id,
            runner_client,
            conversation_store=cast(ConversationStore, store),
        )
        assert handle is not None
        gate.set()
        await asyncio.wait_for(handle.task, timeout=2.0)

        persisted = store.labels.get(session_id)
        assert persisted is not None, f"{expected_code} labels were not persisted"
        assert persisted[sessions_module._LAST_TASK_ERROR_CODE_LABEL_KEY] == expected_code
        assert persisted[sessions_module._LAST_TASK_ERROR_MESSAGE_LABEL_KEY]
        assert sessions_module._last_task_error_from_labels(persisted) == {
            "code": expected_code,
            "message": expected_message,
        }
    finally:
        await _cleanup_relay_test(session_id=session_id, gate=gate, client=client)


@pytest.mark.asyncio
async def test_runner_recovery_clears_persisted_disconnect_error_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Runner recovery drops the persisted ``runner_disconnected`` labels.

    A disconnect persists durable ``last_task_error`` labels so an
    ongoing disconnect still projects a "Disconnected" pill after reload.
    But recovery goes through ``_publish_runner_recovered_status`` — it
    flips the cached ``failed`` back to ``idle`` without a ``running``
    edge, so nothing else clears those labels. Without clearing them here,
    a healthy reconnected-to-idle session keeps reporting
    ``runner_disconnected`` and the Subagents panel keeps the grey dot.
    This asserts recovery clears the labels so the projection returns
    ``None`` again.
    """
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S",
        0.0,
    )
    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    fake_runner = _TunnelCloseRunnerClient(gate)
    store = _RecordingLabelStore()
    session_id = "51af098ee822b1a024acb911f3cdf297"
    # A turn in flight, so the drop below is a genuine interruption and the
    # relay persists the labels this test then asserts recovery clears.
    sessions_module._session_status_cache[session_id] = "running"

    try:
        # Disconnect first: the relay persists the runner_disconnected
        # labels and marks the status cache "failed".
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_recovery_labels",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None
        gate.set()
        await asyncio.wait_for(handle.task, timeout=2.0)

        persisted = store.labels.get(session_id)
        assert persisted is not None
        assert sessions_module._last_task_error_from_labels(persisted) == {
            "code": "runner_disconnected",
            "message": "Runner disconnected unexpectedly.",
        }
        assert sessions_module._session_status_cache.get(session_id) == "failed"

        # Recovery: a successful runner rebind / session-init flips the
        # cached failed back to idle and must drop the durable labels.
        await sessions_module._publish_runner_recovered_status(
            session_id,
            store,  # type: ignore[arg-type]
        )

        assert sessions_module._session_status_cache.get(session_id) == "idle"
        cleared = store.labels.get(session_id)
        assert cleared is not None
        # Both label values are emptied, so the projection collapses back
        # to None — no more runner_disconnected, so no "Disconnected" pill.
        assert cleared[sessions_module._LAST_TASK_ERROR_CODE_LABEL_KEY] == ""
        assert cleared[sessions_module._LAST_TASK_ERROR_MESSAGE_LABEL_KEY] == ""
        assert sessions_module._last_task_error_from_labels(cleared) is None
    finally:
        gate.set()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=1.0)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_relay_suppresses_disconnect_error_on_intentional_stop() -> None:
    """
    A user-initiated Stop drops the tunnel quietly, not as a failure.

    Stopping a host-spawned session tears down its runner tunnel on
    purpose, which makes the relay hit the same ``ConnectionError`` path a
    genuine runner death takes. The Stop handler marks the session in
    ``_intentional_stop_sessions`` first, so the relay must resolve to a
    quiet ``idle`` (no ``runner_disconnected`` status, no persisted error
    labels) rather than rendering "Error · runner_disconnected".
    """
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    fake_runner = _TunnelCloseRunnerClient(gate)
    store = _RecordingLabelStore()
    session_id = "b7c1e2d3f4a5968778695a4b3c2d1e0f"

    collector = None
    try:
        # Simulate the Stop handler: mark the intentional teardown before
        # the tunnel drops.
        sessions_module._intentional_stop_sessions.add(session_id)

        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_intentional_stop",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None

        collector = await start_session_stream_collector(session_id)
        gate.set()
        await asyncio.wait_for(handle.task, timeout=2.0)

        # The relay publishes a quiet idle, never a runner_disconnected failure.
        event = await asyncio.wait_for(collector.queue.get(), timeout=2.0)
        assert event.get("type") == "session.status"
        assert event.get("status") == "idle"
        assert event.get("error") is None

        # The marker is one-shot: consumed by the disconnect handler.
        assert session_id not in sessions_module._intentional_stop_sessions

        # No durable runner_disconnected label persists, so snapshots and
        # child summaries stay clean.
        persisted = store.labels.get(session_id)
        assert persisted is not None
        assert sessions_module._last_task_error_from_labels(persisted) is None
    finally:
        gate.set()
        sessions_module._intentional_stop_sessions.discard(session_id)
        if collector is not None:
            await collector.stop()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=1.0)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


class _ScriptedThenDropStreamResponse:
    """Async stream that emits scripted SSE frames, then raises ``ConnectionError``.

    Unlike ``_ScriptedStreamResponse`` (which closes cleanly with
    ``[DONE]``), this replays scripted frames and then drops the tunnel so
    the relay hits its disconnect handler after processing them.

    :param frames: Ready-to-send ``data: ...`` frames yielded in order
        before the tunnel drop.
    :param gate: Event the test sets once subscribed, gating the frames and
        the drop so the collector observes every scripted frame.
    """

    def __init__(self, frames: list[str], gate: asyncio.Event) -> None:
        self._frames = frames
        self._gate = gate

    async def __aenter__(self) -> _ScriptedThenDropStreamResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    async def aiter_text(self) -> AsyncIterator[str]:
        # The heartbeat comes first so the caller's readiness wait resolves,
        # then everything else waits for the gate: a frame yielded before the
        # test subscribes would be published to nobody, making assertions on
        # the relayed events scheduler-dependent.
        yield 'data: {"type": "session.heartbeat"}\n\n'
        await self._gate.wait()
        for frame in self._frames:
            yield frame
        raise ConnectionError("tunnel closed before request completed")


class _ScriptedThenDropRunnerClient:
    """Fake runner client whose stream replays scripted frames then drops."""

    def __init__(self, frames: list[str], gate: asyncio.Event) -> None:
        self._frames = frames
        self._gate = gate

    def stream(
        self,
        method: str,
        path: str,
        *,
        timeout: Any,
    ) -> _ScriptedThenDropStreamResponse:
        del method, path, timeout
        return _ScriptedThenDropStreamResponse(self._frames, self._gate)


@pytest.mark.asyncio
async def test_relay_running_edge_clears_stale_intentional_stop_marker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A new turn after a Stop must not suppress a later genuine disconnect.

    The relay task is long-lived and reused across turns, and the marker
    set is module-level. A Stop typically emits a terminal
    ``response.cancelled`` (which clears the interrupt fence) before any
    tunnel drop, and a stop that never drops the tunnel leaves the marker
    set. The next turn's ``running`` edge must clear the marker — fence
    membership is already gone — so that a genuine runner death during that
    later turn still surfaces ``runner_disconnected`` rather than being
    silently downgraded to a quiet idle.
    """
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S",
        0.0,
    )
    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    # Terminal stop event clears the fence, then a new turn's running edge
    # must clear the stale intentional-stop marker, then the tunnel drops.
    frames = [
        'data: {"type": "response.cancelled"}\n\n',
        'data: {"type": "session.status", "status": "running"}\n\n',
    ]
    fake_runner = _ScriptedThenDropRunnerClient(frames, gate)
    store = _RecordingLabelStore()
    session_id = "c9d2f3a4b5061728394a5b6c7d8e9f01"

    collector = None
    try:
        # A prior Stop left both markers set (terminal event will clear the
        # fence; the marker must survive to the running edge, then clear).
        sessions_module._interrupt_fenced_sessions.add(session_id)
        sessions_module._intentional_stop_sessions.add(session_id)

        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_stale_marker",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None

        collector = await start_session_stream_collector(session_id)
        gate.set()
        await asyncio.wait_for(handle.task, timeout=2.0)

        # The running edge cleared the marker, so the subsequent tunnel drop
        # is treated as a GENUINE disconnect: failed + runner_disconnected.
        statuses = []
        while not collector.queue.empty():
            statuses.append(await collector.queue.get())
        failed = [e for e in statuses if e.get("status") == "failed"]
        assert failed, f"expected a failed status, saw {statuses}"
        assert failed[-1]["error"]["code"] == "runner_disconnected"

        # And the disconnect cause persisted as durable labels.
        persisted = store.labels.get(session_id)
        assert persisted is not None
        assert sessions_module._last_task_error_from_labels(persisted) == {
            "code": "runner_disconnected",
            "message": "Runner disconnected unexpectedly.",
        }
    finally:
        gate.set()
        sessions_module._interrupt_fenced_sessions.discard(session_id)
        sessions_module._intentional_stop_sessions.discard(session_id)
        if collector is not None:
            await collector.stop()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=1.0)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_relay_stays_quiet_when_runner_leaves_an_idle_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A runner leaving an idle session is not an error.

    A host going away (asleep, restarted, ``omnigent host`` stopped) drops
    the tunnel of every session bound to it, including ones that finished
    their last turn hours ago. The relay used to fail all of them, so those
    sessions rendered a red "The connection to the host dropped
    unexpectedly" banner over a transcript where nothing had been
    interrupted. Scripts a completed turn (``running`` then ``idle``) before
    the drop and asserts the relay publishes no failure and persists no
    error labels — the disconnect surfaces through liveness instead.
    """
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S",
        0.0,
    )
    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    frames = [
        'data: {"type": "session.status", "status": "running"}\n\n',
        'data: {"type": "session.status", "status": "idle"}\n\n',
    ]
    fake_runner = _ScriptedThenDropRunnerClient(frames, gate)
    store = _RecordingLabelStore()
    session_id = "1e2d3c4b5a69788796a5b4c3d2e1f0a9"

    collector = None
    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_idle_disconnect",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None

        collector = await start_session_stream_collector(session_id)
        gate.set()
        await asyncio.wait_for(handle.task, timeout=2.0)

        # Wait for each edge rather than draining a snapshot: the quiet path
        # publishes nothing and awaits nothing, so the relay task can finish
        # before the collector's pump is ever scheduled.
        statuses: list[dict[str, Any]] = []
        while len([e for e in statuses if e.get("type") == "session.status"]) < 2:
            statuses.append(await asyncio.wait_for(collector.queue.get(), timeout=2.0))
        assert [e.get("status") for e in statuses if e.get("type") == "session.status"] == [
            "running",
            "idle",
        ], f"expected only the scripted turn edges, saw {statuses}"

        # The session stays idle: no failed edge for the sidebar badge, and
        # no durable labels for the snapshot to project as a last_task_error
        # (which is what synthesizes the transcript's error block on reload).
        # The cache is written only by ``_publish_status``, so ``idle`` here
        # also proves no failure edge followed the scripted ones.
        assert sessions_module._session_status_cache.get(session_id) == "idle"
        assert sessions_module._last_task_error_from_labels(store.labels[session_id]) is None
    finally:
        gate.set()
        if collector is not None:
            await collector.stop()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=1.0)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_relay_fails_mid_turn_session_from_the_row_when_the_cache_is_cold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A cold status cache falls back to the row, so a restart keeps the failure.

    The in-memory status cache is per-replica and empty after a restart, but
    the relay is re-established for sessions that were mid-turn when the
    server went down (a deploy). Reading only the cache would classify that
    session as idle and swallow a real interruption, leaving the turn hung
    with no error. The durable ``live_status`` on the row is the fallback,
    matching ``_mark_runner_sessions_offline_impl``.
    """
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S",
        0.0,
    )
    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    # No status frames: the relay never caches an edge, exactly as after a
    # restart. The row carries the mid-turn state instead.
    fake_runner = _ScriptedThenDropRunnerClient([], gate)
    store = _RecordingLabelStore(live_status="running")
    session_id = "0f9e8d7c6b5a49382716253445362718"

    try:
        assert sessions_module._session_status_cache.get(session_id) is None

        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_cold_cache",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None

        gate.set()
        await asyncio.wait_for(handle.task, timeout=2.0)

        assert sessions_module._session_status_cache.get(session_id) == "failed"
        persisted = sessions_module._last_task_error_from_labels(store.labels[session_id])
        assert persisted is not None
        assert persisted["code"] == "runner_disconnected"
    finally:
        gate.set()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=1.0)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_relay_reports_the_drop_when_the_live_status_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An unreadable row reports the drop instead of killing the relay.

    The cold-cache fallback reads the row from inside the disconnect
    handler. A store error there must not escape: an exception thrown out of
    that handler ends the relay task before either branch publishes,
    truncating the client's stream with no error event — exactly what the
    ``failed`` status exists to prevent. An indeterminate answer therefore
    reports the drop, as the ungated relay always did.
    """
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S",
        0.0,
    )
    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    fake_runner = _ScriptedThenDropRunnerClient([], gate)
    store = _RecordingLabelStore()
    monkeypatch.setattr(
        store,
        "get_conversation",
        lambda conversation_id: (_ for _ in ()).throw(RuntimeError("db blip")),
    )
    session_id = "abcdef0123456789abcdef0123456789"

    try:
        assert sessions_module._session_status_cache.get(session_id) is None

        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_unreadable_row",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None

        gate.set()
        await asyncio.wait_for(handle.task, timeout=2.0)

        # The relay survived the store error and still reported the cause.
        assert handle.task.exception() is None
        assert sessions_module._session_status_cache.get(session_id) == "failed"
        persisted = sessions_module._last_task_error_from_labels(store.labels[session_id])
        assert persisted is not None
        assert persisted["code"] == "runner_disconnected"
    finally:
        gate.set()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=1.0)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


class _FlakyThenHealthyRunnerClient:
    """Fake runner client that raises scripted failures, then recovers."""

    def __init__(self, failures: tuple[Exception, ...]) -> None:
        self._failures = failures
        self.calls = 0

    def stream(
        self,
        method: str,
        path: str,
        *,
        timeout: Any,
    ) -> _HeartbeatStreamResponse:
        del method, path, timeout
        self.calls += 1
        if self.calls <= len(self._failures):
            raise self._failures[self.calls - 1]
        release = asyncio.Event()
        release.set()
        return _HeartbeatStreamResponse(release)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failures",
    [
        pytest.param(
            (ConnectionError("tunnel closed before request completed"),),
            id="tunnel-drop",
        ),
        pytest.param(
            (
                ConnectionError("tunnel closed before request completed"),
                httpx.ConnectError(
                    "runner is not registered yet",
                    request=httpx.Request("GET", "http://runner/stream"),
                ),
            ),
            id="offline-gap",
        ),
    ],
)
async def test_relay_retries_transport_errors_within_grace(
    monkeypatch: pytest.MonkeyPatch,
    failures: tuple[Exception, ...],
) -> None:
    """Tunnel loss and its offline gap stay retriable inside the grace."""
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration._RELAY_RETRY_INTERVAL_S",
        0.01,
    )
    sessions_module._runner_relay_tasks.clear()
    fake_runner = _FlakyThenHealthyRunnerClient(failures)
    store = _RecordingLabelStore()
    session_id = "5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d"

    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_flaky_then_healthy",
            cast(httpx.AsyncClient, fake_runner),
            conversation_store=cast(ConversationStore, store),
        )
        assert handle is not None
        await asyncio.wait_for(handle.task, timeout=2.0)

        assert fake_runner.calls == len(failures) + 1
        assert sessions_module._session_status_cache.get(session_id) is None
        assert store.labels.get(session_id) is None
    finally:
        await _cleanup_relay_test(session_id=session_id)


def _bound_conv(
    session_id: str,
    *,
    kind: str = "default",
    live_status: str | None = None,
) -> Any:
    """
    Build a conversation-shaped row for the offline-reconciliation helper.

    ``_mark_runner_sessions_offline`` reads only ``id``, ``kind`` and
    ``live_status`` off each row, so a namespace is enough.

    :param session_id: Conversation identifier.
    :param kind: ``"default"`` (top-level) or ``"sub_agent"``.
    :param live_status: Persisted live status, read only on a cache miss.
    :returns: A conversation-shaped namespace.
    """
    return SimpleNamespace(id=session_id, kind=kind, live_status=live_status)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "cached", "live_status", "intentional_stop", "fail_idle_top_level", "expect_failed"),
    [
        # A turn was in flight when the runner went away — fail it, with cause.
        ("default", "running", None, False, False, True),
        ("sub_agent", "running", None, False, False, True),
        ("sub_agent", "waiting", None, False, False, True),
        # A sub-agent that finished its work keeps that outcome: the runner
        # leaving does not retroactively fail completed work (this is the
        # whole Agents-rail-goes-red bug).
        ("sub_agent", "idle", None, False, False, False),
        ("default", "idle", None, False, False, False),
        # Cache miss falls back to the persisted row value.
        ("default", None, "running", False, False, True),
        ("default", None, "idle", False, False, False),
        ("default", None, None, False, False, False),
        # Stop / archive drop the tunnel on purpose; the relay owns that path.
        ("default", "running", None, True, False, False),
        # A crash report also covers the runner that died before it could run
        # anything, so an idle TOP-LEVEL session is failed — but an idle
        # sub-agent (spawned by an already-live runner) still is not.
        ("default", "idle", None, False, True, True),
        ("sub_agent", "idle", None, False, True, False),
        # A crash report never downgrades an interrupted turn: a mid-turn
        # sub-agent is failed under either flag.
        ("sub_agent", "waiting", None, False, True, True),
        # An intentional teardown still wins over the crash-report flag.
        ("default", "idle", None, True, True, False),
    ],
)
async def test_mark_runner_sessions_offline_only_fails_interrupted_turns(
    kind: str,
    cached: str | None,
    live_status: str | None,
    intentional_stop: bool,
    fail_idle_top_level: bool,
    expect_failed: bool,
) -> None:
    """
    Only the sessions a departed runner interrupted are failed, with cause.

    Sub-agents ride their parent's runner, so a drop reaches every child
    bound to it. Marking them all ``failed`` painted the whole Agents rail
    red for sub-agents that had completed successfully, and — because the
    fan-out carried no ``ErrorDetail`` — left a failure the UI could not
    tell from a real one and the reconnect recovery could not clear.
    """
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.schemas import ErrorDetail

    session_id = "b04d1f3c9a5e4f7a8c2b6d0e1f3a5c79"
    store = _RecordingLabelStore()
    error = ErrorDetail(code="runner_disconnected", message="Runner disconnected unexpectedly.")
    if cached is not None:
        sessions_module._session_status_cache[session_id] = cached
    if intentional_stop:
        sessions_module._intentional_stop_sessions.add(session_id)

    try:
        await sessions_module._mark_runner_sessions_offline(
            [_bound_conv(session_id, kind=kind, live_status=live_status)],
            error,
            store,  # type: ignore[arg-type]
            fail_idle_top_level=fail_idle_top_level,
        )

        status = sessions_module._session_status_cache.get(session_id)
        persisted = store.labels.get(session_id)
        if expect_failed:
            assert status == "failed"
            # The cause must be durable: it is what lets the UI render a
            # benign "Disconnected" and what
            # ``_publish_runner_recovered_status`` matches on to clear the
            # failure when the runner comes back.
            assert persisted is not None
            assert sessions_module._last_task_error_from_labels(persisted) == {
                "code": "runner_disconnected",
                "message": "Runner disconnected unexpectedly.",
            }
        else:
            assert status == cached
            assert persisted is None
    finally:
        sessions_module._intentional_stop_sessions.discard(session_id)
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_relay_does_not_fail_turn_during_server_shutdown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A stream drop while THIS server is shutting down leaves the turn alone.

    Shutdown closes the runner tunnels, which drops every relay stream; the
    runner itself is alive and reconnects to the replacement server. The
    give-up path must publish no ``failed`` status and persist no
    ``runner_disconnected`` labels for that self-inflicted loss.
    """
    from omnigent.runtime import session_stream
    from omnigent.server import shutdown_state
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S",
        0.0,
    )
    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    fake_runner = _TunnelCloseRunnerClient(gate)
    store = _RecordingLabelStore(live_status="running")
    session_id = "5b1e2d7c9a4f4e0b8c3d2a1f6e7d8c9b"
    sessions_module._session_status_cache[session_id] = "running"
    shutdown_state.mark_server_shutting_down()

    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_server_shutdown",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None
        gate.set()
        await asyncio.wait_for(handle.task, timeout=2.0)

        assert session_id not in store.labels, "shutdown-time drop persisted failure labels"
        assert sessions_module._session_status_cache.get(session_id) == "running"
    finally:
        shutdown_state.reset_for_tests()
        gate.set()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=1.0)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_relay_real_transport_read_timeout_stamps_session_stream_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled stream through the REAL transport stamps ``session_stream_lost``.

    End-to-end reachability: the relay's per-request read timeout must be
    honored by :class:`WSTunnelTransport` itself (no hand-injected error),
    raise ``httpx.ReadTimeout`` on silence, and leave the tunnel registered
    so attribution resolves to ``session_stream_lost``. The reconnect
    grace is zeroed so the drop is terminal on the first attempt.
    """
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import orchestration

    monkeypatch.setattr(orchestration, "_RELAY_STREAM_READ_TIMEOUT_S", 0.5)
    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 0.0)
    sessions_module._runner_relay_tasks.clear()
    runner_id = "runner_stream_stall_real_transport"
    registry, client = _registered_tunnel_client(runner_id)
    session_id = "d4e5f60718293a4b5c6d7e8f90a1b2c3"

    # Feed the head + one heartbeat through the real registry, then go
    # silent — the transport's read timeout must fire on its own.
    feeder = asyncio.create_task(_feed_tunnel_sse_start(registry, runner_id))
    collector = None
    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            runner_id,
            client,
            conversation_store=None,
        )
        assert handle is not None
        collector = await start_session_stream_collector(session_id)

        await asyncio.wait_for(handle.task, timeout=5.0)

        event = await asyncio.wait_for(collector.queue.get(), timeout=2.0)
        assert event.get("type") == "session.status"
        assert event.get("status") == "failed"
        assert event["error"]["code"] == "session_stream_lost"
        # The tunnel itself never went down.
        assert registry.get(runner_id) is not None
    finally:
        await _cleanup_relay_test(
            session_id=session_id,
            collector=collector,
            client=client,
            feeder=feeder,
        )


@pytest.mark.asyncio
async def test_runner_tunnel_alive_resolves_on_real_routed_client() -> None:
    """Pin ``_runner_tunnel_alive`` against a real routed client.

    The helper resolves the client's transport and asks its public
    ``runner_registered`` probe; this test constructs a real routed
    client so a transport/router refactor breaks loudly here instead of
    silently misattributing disconnects.
    """
    from omnigent.runner.routing import RunnerRouter
    from omnigent.server.routes._sessions.orchestration import _runner_tunnel_alive

    runner_id = "runner_liveness_pin"
    registry = TunnelRegistry()
    _register_test_runner(registry, runner_id)
    store = SimpleNamespace(
        get_conversation=lambda conversation_id: SimpleNamespace(runner_id=runner_id)
    )
    router = RunnerRouter(
        registry=registry,
        conversation_store=cast(ConversationStore, store),
    )

    try:
        routed = router.client_for_existing_conversation("conv_liveness_pin")
        assert routed is not None
        transport = cast(WSTunnelTransport, routed.client._transport)
        assert transport.runner_registered() is True
        assert _runner_tunnel_alive(routed.client, runner_id) is True

        registry.deregister(runner_id)
        assert transport.runner_registered() is False
        assert _runner_tunnel_alive(routed.client, runner_id) is False
    finally:
        await router.aclose()
