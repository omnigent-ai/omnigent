"""Tests for AP's runner stream relay startup handshake."""

from __future__ import annotations

import asyncio
import contextlib
import json
import threading
from collections.abc import AsyncIterator
from pathlib import Path
from types import TracebackType
from typing import Any

import httpx
import pytest

from omnigent.entities import Conversation
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.debug_log_helpers import capture_debug_rows
from tests.server.helpers import start_session_stream_collector

# Wall-clock ceiling for awaiting relay tasks / stream events. Generous on
# purpose: the relay runs as a background task on a shared, 8-worker xdist
# runner, and a tight budget (1-2s) times out under CPU contention while a
# passing test never waits this long.
_TASK_TIMEOUT_S = 10.0


class _HeartbeatStreamResponse:
    """
    Async context manager that mimics ``httpx.AsyncClient.stream``.

    :param release: Event that lets the fake stream finish after the
        ready heartbeat has been consumed.
    """

    def __init__(self, release: asyncio.Event, *, drop: bool = False) -> None:
        """
        Initialize the fake streaming response.

        :param release: Event used to unblock the stream tail.
        :param drop: Raise a transport error after the gate instead of
            ending the stream with ``[DONE]``.
        """
        self._release = release
        self._drop = drop

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

    def raise_for_status(self) -> None:
        """The scripted stream represents a successful HTTP response."""

    async def aiter_text(self) -> AsyncIterator[str]:
        """
        Yield a ready heartbeat, then finish or drop after release.

        :yields: SSE text chunks in the same data-line shape the runner
            emits over HTTP.
        """
        yield 'data: {"type": "session.heartbeat"}\n\n'
        await self._release.wait()
        if self._drop:
            raise ConnectionError("tunnel closed before request completed")
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
        with capture_debug_rows("server") as rows:
            handle = await sessions_module._ensure_runner_relay_ready(
                "a7f039e9f1311474878eb7d4699c1013",
                "runner_ready",
                fake_runner,  # type: ignore[arg-type]
                conversation_store=None,
            )

        assert handle is not None
        assert handle.ready.is_set()
        ready_row = next(row for row in rows if row["event_name"] == "runner_stream_ready")
        assert ready_row["session_id"] == "a7f039e9f1311474878eb7d4699c1013"
        assert ready_row["attributes"]["runner_id"] == "runner_ready"
        assert ready_row["attributes"]["telemetry_schema"] == "runner_stream_recovery.v1"
        connected_row = next(row for row in rows if row["event_name"] == "runner_stream_connected")
        assert connected_row["attributes"]["telemetry_schema"] == "runner_stream_recovery.v1"
        assert not any(row["event_name"] == "runner_stream_recovered" for row in rows)
        assert fake_runner.stream_calls[0][0] == "GET"
        assert (
            fake_runner.stream_calls[0][1]
            == "/v1/sessions/a7f039e9f1311474878eb7d4699c1013/stream"
        )
    finally:
        release.set()
        handle = sessions_module._runner_relay_tasks.get("a7f039e9f1311474878eb7d4699c1013")
        if handle is not None:
            await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
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

    def raise_for_status(self) -> None:
        """The scripted stream represents a successful HTTP response."""

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


@pytest.mark.parametrize("outcome", ["completed", "failed", "cancelled"])
@pytest.mark.asyncio
async def test_subagent_activity_waits_for_final_idle_after_buffered_turns(
    db_uri: str, outcome: str
) -> None:
    from omnigent.server.routes._sessions.orchestration import _relay_runner_stream_once

    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(
        parent_conversation_id=parent.id, title="researcher:Auth audit"
    )
    release = asyncio.Event()
    release.set()
    parent_events = [
        {"type": "response.in_progress", "response": {"id": "parent-turn", "model": "test"}},
        {"type": "response.output_text.delta", "delta": "I will ask a researcher."},
        {"type": "session.created", "child_session_id": child.id},
        {"type": "session.created", "child_session_id": child.id},
    ]
    await _relay_runner_stream_once(
        parent.id,
        _ScriptedRunnerClient(release, parent_events),
        store,  # type: ignore[arg-type]
    )
    initial = store.list_items(parent.id).data
    assert [item.type for item in initial] == ["message", "resource_event"]
    assert initial[1].data.resource == {"title": "Auth audit"}

    child_events = [
        {"type": "response.in_progress", "response": {"id": "first", "model": "test"}},
        {"type": "response.completed", "response": {"id": "first"}},
        {"type": "response.in_progress", "response": {"id": "second", "model": "test"}},
        {"type": "response.output_text.delta", "delta": "Finished the full task."},
        {"type": f"response.{outcome}", "response": {"id": "second"}},
        {"type": "session.status", "status": "failed" if outcome == "failed" else "idle"},
        {"type": "session.status", "status": "failed" if outcome == "failed" else "idle"},
    ]
    await _relay_runner_stream_once(
        child.id,
        _ScriptedRunnerClient(release, child_events),
        store,  # type: ignore[arg-type]
    )
    items = store.list_items(parent.id, type="resource_event").data
    assert [item.data.event_type for item in items] == [
        "session.subagent.delegated",
        "session.subagent.returned",
    ]
    assert items[-1].data.resource["status"] == outcome


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
            await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._runner_relay_tasks.clear()
        session_stream.close(session_id)


def test_context_labels_from_turn_usage_shapes() -> None:
    """The label builder is in-process-only and degrades gracefully.

    * ``context_tokens`` + a resolvable model → both labels.
    * a turn with no ``context_tokens`` → empty (native harnesses post their
      own usage, so this path must not double-write their labels).
    * ``context_tokens`` but an unknown/blank model → numerator only (the ring
      simply won't render without a denominator, rather than guessing one).
    """
    from omnigent.llms.context_window import get_model_context_window
    from omnigent.server.routes.sessions import _context_labels_from_turn_usage

    both = _context_labels_from_turn_usage({"context_tokens": 1234, "model": "claude-sonnet-5"})
    assert both["omnigent.last_context_tokens"] == "1234"
    assert both["omnigent.last_context_window"] == str(get_model_context_window("claude-sonnet-5"))

    # No window-fill signal → no labels (native external_session_usage owns it).
    assert _context_labels_from_turn_usage({"input_tokens": 10, "output_tokens": 5}) == {}
    assert _context_labels_from_turn_usage({}) == {}

    # A negative/invalid count is not a real fill signal.
    negative = _context_labels_from_turn_usage({"context_tokens": -1, "model": "claude-sonnet-5"})
    assert negative == {}

    # Numerator without a resolvable model → tokens only.
    no_model = _context_labels_from_turn_usage({"context_tokens": 42})
    assert no_model == {"omnigent.last_context_tokens": "42"}


@pytest.mark.asyncio
async def test_relay_persists_context_window_labels_for_inprocess_turn(db_uri: str) -> None:
    """
    An in-process turn's usage fills the context-window indicator.

    A claude-sdk (or any in-process) turn reports ``context_tokens`` (window
    fill) and its observed ``model`` on ``response.completed``. The relay must
    persist both context labels — ``omnigent.last_context_tokens`` (numerator)
    and ``omnigent.last_context_window`` (denominator, resolved from the
    model's window) — and publish them on the live ``session.usage`` event, so
    the web context ring renders live AND survives a reload/snapshot. Before
    this, only the claude-native external_session_usage POST wrote those
    labels, leaving a model-unpinned claude-sdk session with no ring.
    """
    from omnigent.llms.context_window import get_model_context_window
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    sessions_module._runner_relay_tasks.clear()
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation()
    session_id = conv.id

    # Denominator is the observed model's catalog window, computed the same way
    # the relay does (the resolved value varies with catalog availability, so
    # derive it rather than hard-coding a token count).
    expected_window = get_model_context_window("claude-sonnet-5")

    response_id = "resp_ctx_ring_1"
    # The observed model (a full id, not a bare alias) is what the SDK reports.
    turn_events: list[dict[str, Any]] = [
        {"type": "response.in_progress", "response": {"id": response_id, "model": "jarvis"}},
        {
            "type": "response.completed",
            "response": {
                "id": response_id,
                "model": "jarvis",
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "total_tokens": 1200,
                    "context_tokens": 45678,
                    "model": "claude-sonnet-5",
                },
            },
        },
    ]
    release = asyncio.Event()
    fake_runner = _ScriptedRunnerClient(release, turn_events)

    collector = None
    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_relay_ctx_ring",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,
        )
        assert handle is not None
        collector = await start_session_stream_collector(session_id)
        release.set()

        # Drain to the session.usage event carrying the context fields.
        usage_event: dict[str, Any] | None = None
        for _ in range(50):
            event = await collector.next_event()
            if event["type"] == "session.usage" and "context_tokens" in event:
                usage_event = event
                break
        assert usage_event is not None, "no session.usage event carried context_tokens"
        assert usage_event["context_tokens"] == 45678
        assert usage_event["context_window"] == expected_window

        # Labels are persisted (durable across reload/snapshot).
        refreshed = store.get_conversation(session_id)
        assert refreshed is not None
        assert refreshed.labels.get("omnigent.last_context_tokens") == "45678"
        assert refreshed.labels.get("omnigent.last_context_window") == str(expected_window)
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

    def raise_for_status(self) -> None:
        """The scripted stream represents a successful HTTP response."""

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


@pytest.mark.parametrize("failure_path", ["relay", "sweep", "sweep_after_restart"])
@pytest.mark.parametrize("terminal_response", [False, True])
@pytest.mark.asyncio
async def test_relay_publishes_failed_status_on_tunnel_close(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_path: str,
    terminal_response: bool,
) -> None:
    """A confirmed disconnect persists one Failed notice even without child output."""
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.schemas import ErrorDetail
    from omnigent.server.subagent_activity import record_subagent_activity

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S",
        0.0,
    )
    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    events = [{"type": "response.in_progress", "response": {"id": "child-turn"}}]
    if terminal_response:
        events.append({"type": "response.completed", "response": {"id": "child-turn"}})
    fake_runner = (
        _ScriptedThenDropRunnerClient([f"data: {json.dumps(event)}\n\n" for event in events], gate)
        if failure_path == "relay"
        else _ScriptedRunnerClient(gate, events)
    )
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(parent_conversation_id=parent.id, title="researcher:Audit")
    session_id = child.id
    await record_subagent_activity(session_id, "delegated", store)
    sessions_module._session_status_cache[session_id] = "running"

    collector = None
    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_tunnel_close",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,
        )
        assert handle is not None

        # Subscribe BEFORE releasing the error so the published
        # session.status event fans out to the collector.
        collector = await start_session_stream_collector(session_id)
        gate.set()

        # The relay task should finish quickly after the ConnectionError.
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        if failure_path != "relay":
            if failure_path == "sweep_after_restart":
                sessions_module._session_active_response_cache.pop(session_id, None)
            await sessions_module._mark_runner_sessions_offline(
                [child], ErrorDetail(code="runner_disconnected", message="Disconnected"), store
            )

        # Wait for the failed-status event to arrive at the collector.
        event = await asyncio.wait_for(collector.queue.get(), timeout=_TASK_TIMEOUT_S)
        while event.get("type") != "session.status":
            event = await asyncio.wait_for(collector.queue.get(), timeout=_TASK_TIMEOUT_S)
        assert event.get("status") == "failed"
        assert event["error"]["code"] == "runner_disconnected"
        items = store.list_items(parent.id).data
        assert [item.data.event_type for item in items] == [
            "session.subagent.delegated",
            "session.subagent.returned",
        ]
        assert items[-1].data.resource == {"title": "Audit", "status": "failed"}
        await record_subagent_activity(
            session_id,
            "returned",
            store,
            status="failed",
            turn_id=child.id if failure_path == "sweep_after_restart" else "child-turn",
        )
        assert len(store.list_items(parent.id).data) == 2
        if failure_path == "relay":
            record = next(
                r
                for r in caplog.records
                if getattr(r, "event_name", None) == "runner_stream_disconnected"
            )
            assert record.session_id == session_id
            assert record.attributes["intentional_stop"] is False
            assert record.attributes["cached_session_status"] == "running"
            assert record.attributes["decision"] == "failed_mid_turn"
            assert record.exc_info is not None
    finally:
        gate.set()
        if collector is not None:
            await collector.stop()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        sessions_module._session_active_response_cache.pop(session_id, None)
        session_stream.close(session_id)


class _RecordingLabelStore:
    """Minimal store for disconnect labels, live status, and runner liveness.

    :param runner_liveness: Canned runner bindings and heartbeats used to
        simulate a runner live on another replica.
    """

    def __init__(
        self,
        *,
        live_status: str = "idle",
        runner_liveness: dict[str, tuple[str | None, int | None]] | None = None,
    ) -> None:
        self.labels: dict[str, dict[str, str]] = {}
        self.live_status = live_status
        self._runner_liveness = runner_liveness or {}

    def set_labels(self, conversation_id: str, updates: dict[str, str]) -> None:
        self.labels.setdefault(conversation_id, {}).update(updates)

    def get_runner_liveness(self, conversation_id: str) -> tuple[str | None, int | None] | None:
        return self._runner_liveness.get(conversation_id)

    def get_conversation(self, conversation_id: str) -> Conversation:
        """Return a conversation-shaped object exposing the read fields.

        ``.labels`` is read by the recovery guard, ``.live_status`` by the
        mid-turn check when the in-memory status cache is cold.
        """
        return Conversation(
            id=conversation_id,
            root_conversation_id=conversation_id,
            created_at=0,
            updated_at=0,
            labels=dict(self.labels.get(conversation_id, {})),
            live_status=self.live_status,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("new_turn_without_identity", [False, True])
async def test_relay_captures_failure_agent_without_reusing_prior_turn_identity(
    new_turn_without_identity: bool,
) -> None:
    """A status-only failure retains its own turn's name, never a prior turn's."""
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    session_id = "c08158bd064c4f32b4e435414a936711"
    events: list[dict[str, Any]] = [
        {"type": "session.status", "status": "running"},
        {"type": "response.in_progress", "response": {"id": "resp_one", "model": "nessie"}},
    ]
    if new_turn_without_identity:
        events.append({"type": "session.status", "status": "running"})
    events.append(
        {
            "type": "session.status",
            "status": "failed",
            "error": {"code": "executor_error", "message": "Harness stopped."},
        }
    )
    gate = asyncio.Event()
    gate.set()
    store = _RecordingLabelStore()
    try:
        await sessions_module._relay_runner_stream(
            session_id,
            _ScriptedRunnerClient(gate, events),  # type: ignore[arg-type]
            store,  # type: ignore[arg-type]
        )
        error = sessions_module._last_task_error_from_labels(store.labels[session_id])
        assert error is not None
        assert error.get("agent_name") == (None if new_turn_without_identity else "nessie")
        assert error["message"] == "Harness stopped."
    finally:
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_relay_persists_disconnect_error_labels_on_tunnel_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A tunnel close mid-turn persists the ``runner_disconnected`` cause as labels.

    Option B: a runner that merely disconnected must be distinguishable
    from a genuine task failure. The relay-fed status cache only carries a
    generic ``failed``, so the disconnect cause is preserved as durable
    ``last_task_error`` labels — these survive into snapshots and child
    summaries, letting the UI render a "Disconnected" pill (not red
    "Failed"). The code must be ``runner_disconnected`` so the UI can
    branch on it before the generic failed path.
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
    session_id = "82fe36b7ca1bfb567bfbcce4eaa487a1"
    # Only an interrupted turn is failed by the drop, so put one in flight.
    sessions_module._session_status_cache[session_id] = "running"

    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_tunnel_close_labels",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None
        gate.set()

        # The relay task should finish quickly after the ConnectionError.
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

        persisted = store.labels.get(session_id)
        assert persisted is not None, "disconnect did not persist failure labels"
        assert persisted[sessions_module._LAST_TASK_ERROR_CODE_LABEL_KEY] == "runner_disconnected"
        # The message is non-empty so the projection surfaces a typed
        # ``last_task_error`` (both code and message are required there).
        assert persisted[sessions_module._LAST_TASK_ERROR_MESSAGE_LABEL_KEY]

        # The persisted labels project back to a code-preserving
        # ``last_task_error`` — proving the disconnect cause is NOT
        # collapsed into an indistinguishable generic failure.
        projected = sessions_module._last_task_error_from_labels(persisted)
        assert projected == {
            "code": "runner_disconnected",
            "message": "Runner disconnected unexpectedly.",
        }
    finally:
        gate.set()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


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
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

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
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_relay_suppresses_disconnect_error_on_intentional_stop(
    caplog: pytest.LogCaptureFixture,
) -> None:
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
        sessions_module._intentional_stop_sessions[session_id] = "runner_intentional_stop"

        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_intentional_stop",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None

        collector = await start_session_stream_collector(session_id)
        gate.set()
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

        # The relay publishes a quiet idle, never a runner_disconnected failure.
        event = await asyncio.wait_for(collector.queue.get(), timeout=_TASK_TIMEOUT_S)
        assert event.get("type") == "session.status"
        assert event.get("status") == "idle"
        assert event.get("error") is None

        # The marker is one-shot: consumed by the disconnect handler.
        assert session_id not in sessions_module._intentional_stop_sessions
        record = next(
            r
            for r in caplog.records
            if getattr(r, "event_name", None) == "runner_stream_disconnected"
        )
        assert record.session_id == session_id
        assert record.attributes["intentional_stop"] is True
        assert record.attributes["cached_session_status"] is None
        assert record.attributes["decision"] == "intentional_stop"

        # No durable runner_disconnected label persists, so snapshots and
        # child summaries stay clean.
        persisted = store.labels.get(session_id)
        assert persisted is not None
        assert sessions_module._last_task_error_from_labels(persisted) is None
    finally:
        gate.set()
        sessions_module._intentional_stop_sessions.pop(session_id, None)
        if collector is not None:
            await collector.stop()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
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

    def raise_for_status(self) -> None:
        """The scripted stream represents a successful HTTP response."""

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
@pytest.mark.parametrize("intentional", [False, True])
async def test_relay_preserves_failure_reported_during_intentional_teardown(
    monkeypatch: pytest.MonkeyPatch,
    intentional: bool,
) -> None:
    """A failure arriving after stop intent keeps its status and durable details."""
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S", 0.0
    )
    session_id = "aa251103f45c42da8b471d1f6b12b54a"
    runner_id = "runner-failing-during-stop"
    error = {"code": "native_turn_error", "message": "Harness failed before teardown."}
    event = {"type": "session.status", "status": "failed", "error": error}
    gate = asyncio.Event()
    runner = _ScriptedThenDropRunnerClient([f"data: {json.dumps(event)}\n\n"], gate)
    store = _RecordingLabelStore(live_status="running")
    sessions_module._session_status_cache[session_id] = "running"
    collector = None
    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            runner_id,
            runner,  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None
        collector = await start_session_stream_collector(session_id)
        if intentional:
            sessions_module._intentional_stop_sessions[session_id] = runner_id
        gate.set()
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

        status = await asyncio.wait_for(collector.queue.get(), timeout=_TASK_TIMEOUT_S)
        assert status["type"] == "session.status"
        assert status["status"] == "failed"
        assert status["error"]["code"] == error["code"]
        assert sessions_module._session_status_cache.get(session_id) == "failed"
        persisted = sessions_module._last_task_error_from_labels(store.labels[session_id])
        assert persisted is not None
        assert persisted["code"] == error["code"]
        assert persisted["message"] == error["message"]
        assert session_id not in sessions_module._intentional_stop_sessions
    finally:
        gate.set()
        if collector is not None:
            await collector.stop()
        handle = sessions_module._runner_relay_tasks.pop(session_id, None)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._intentional_stop_sessions.pop(session_id, None)
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("mark_before_rebind", [True, False])
async def test_relay_ignores_stop_intent_for_a_different_runner(
    monkeypatch: pytest.MonkeyPatch, mark_before_rebind: bool
) -> None:
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S", 0.0
    )
    session_id = "rebound-stop-session"
    gate = asyncio.Event()
    store = _RecordingLabelStore()
    sessions_module._session_status_cache[session_id] = "running"
    if mark_before_rebind:
        sessions_module._intentional_stop_sessions[session_id] = "runner-old"
    handle = await sessions_module._ensure_runner_relay_ready(
        session_id,
        "runner-new",
        _TunnelCloseRunnerClient(gate),  # type: ignore[arg-type]
        conversation_store=store,  # type: ignore[arg-type]
    )
    assert handle is not None
    try:
        assert session_id not in sessions_module._intentional_stop_sessions
        if not mark_before_rebind:
            # A late old-runner teardown cannot apply to the new relay either.
            sessions_module._intentional_stop_sessions[session_id] = "runner-old"
        gate.set()
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        assert sessions_module._session_status_cache[session_id] == "failed"
        error = sessions_module._last_task_error_from_labels(store.labels[session_id])
        assert error is not None
        assert error["code"] == "runner_disconnected"
    finally:
        gate.set()
        handle.task.cancel()
        await asyncio.gather(handle.task, return_exceptions=True)
        sessions_module._intentional_stop_sessions.pop(session_id, None)
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_cancelled_old_relay_preserves_replacement_runner_stop() -> None:
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    session_id = "replacement-stop-session"
    old_gate, new_gate = asyncio.Event(), asyncio.Event()
    store = _RecordingLabelStore()
    old_handle = await sessions_module._ensure_runner_relay_ready(
        session_id,
        "runner-old",
        _TunnelCloseRunnerClient(old_gate),  # type: ignore[arg-type]
        conversation_store=store,  # type: ignore[arg-type]
    )
    assert old_handle is not None
    new_handle = sessions_module._ensure_runner_relay(
        session_id,
        "runner-new",
        _TunnelCloseRunnerClient(new_gate),  # type: ignore[arg-type]
        conversation_store=store,  # type: ignore[arg-type]
    )
    assert new_handle is not None
    sessions_module._intentional_stop_sessions[session_id] = "runner-new"
    try:
        await asyncio.wait_for(
            asyncio.gather(old_handle.task, return_exceptions=True), timeout=_TASK_TIMEOUT_S
        )
        assert sessions_module._intentional_stop_sessions.get(session_id) == "runner-new"
        new_gate.set()
        await asyncio.wait_for(new_handle.task, timeout=_TASK_TIMEOUT_S)
        assert sessions_module._session_status_cache[session_id] == "idle"
        assert sessions_module._last_task_error_from_labels(store.labels[session_id]) is None
    finally:
        old_gate.set()
        new_gate.set()
        new_handle.task.cancel()
        await asyncio.gather(new_handle.task, return_exceptions=True)
        sessions_module._intentional_stop_sessions.pop(session_id, None)
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("live_status", ["waiting", "running", "idle"])
async def test_relay_same_turn_running_preserves_intentional_stop(
    monkeypatch: pytest.MonkeyPatch,
    live_status: str,
) -> None:
    """Resuming work or PTY activity during teardown must keep stop intent."""
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S", 0.0
    )
    session_id = "child-resuming-during-stop"
    runner_id = "runner-intentional-stop"
    gate = asyncio.Event()
    frames = ['data: {"type": "session.status", "status": "running"}\n\n']
    store = _RecordingLabelStore(live_status=live_status)
    sessions_module._session_status_cache[session_id] = live_status
    sessions_module._intentional_stop_sessions[session_id] = runner_id
    collector = None
    handle = None
    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            runner_id,
            _ScriptedThenDropRunnerClient(frames, gate),  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None
        collector = await start_session_stream_collector(session_id)
        gate.set()
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        statuses = [
            await collector.next_event(timeout=_TASK_TIMEOUT_S),
            await collector.next_event(timeout=_TASK_TIMEOUT_S),
        ]
        assert not any(event.get("status") == "failed" for event in statuses), statuses
        assert statuses[-1].get("status") == "idle"
        assert sessions_module._session_status_cache[session_id] == "idle"
        assert sessions_module._last_task_error_from_labels(store.labels[session_id]) is None
        assert session_id not in sessions_module._intentional_stop_sessions
    finally:
        gate.set()
        if collector is not None:
            await collector.stop()
        if handle is not None:
            handle.task.cancel()
            await asyncio.gather(handle.task, return_exceptions=True)
        sessions_module._runner_relay_tasks.pop(session_id, None)
        sessions_module._intentional_stop_sessions.pop(session_id, None)
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("earlier_stop", "outcome"),
    [
        ("none", "acknowledged"),
        ("rolled_back", "acknowledged"),
        ("retained", "acknowledged"),
        ("retained", "timeout"),
        ("retained", "rejected"),
        ("retained", "rejected_while_running"),
    ],
)
async def test_relay_terminal_observation_tracks_stop_attempt(
    monkeypatch: pytest.MonkeyPatch,
    db_uri: str,
    earlier_stop: str,
    outcome: str,
) -> None:
    """A new Stop resets terminal observation, while rejection restores old intent."""
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.routes._sessions import orchestration
    from omnigent.server.routes._sessions.helpers import _HostRunnerStopAttempt

    dispatched = asyncio.Event()
    acknowledgement = asyncio.Event()

    async def stop_host(*_args: object, attempt: _HostRunnerStopAttempt) -> bool:
        attempt.dispatched = True
        dispatched.set()
        if outcome == "rejected_while_running":
            await acknowledgement.wait()
        attempt.rejected = outcome.startswith("rejected")
        return outcome == "acknowledged"

    monkeypatch.setattr(orchestration, "RUNNER_DISCONNECT_GRACE_S", 0.0)
    monkeypatch.setattr(sessions_module, "_stop_session_host_runner", stop_host)
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(kind="sub_agent", parent_conversation_id=parent.id)
    session_id = child.id
    runner_id = "runner-stop-after-terminal"
    for row in (parent, child):
        store.set_runner_id(row.id, runner_id)
        store.set_session_live_status(row.id, "waiting")
        sessions_module._session_status_cache[row.id] = "waiting"
    if earlier_stop != "none":
        sessions_module._intentional_stop_sessions[session_id] = runner_id
    gate = asyncio.Event()
    runner = _ScriptedThenDropRunnerClient([], gate)
    response = _ScriptedThenDropStreamResponse([], gate)
    stop_task: asyncio.Task[bool] | None = None

    async def stream_events() -> AsyncIterator[str]:
        nonlocal stop_task

        yield 'data: {"type": "session.heartbeat"}\n\n'
        await gate.wait()
        yield 'data: {"type": "response.cancelled"}\n\n'
        if earlier_stop == "rolled_back":
            sessions_module._intentional_stop_sessions.pop(session_id, None)
        stop_task = asyncio.create_task(
            orchestration._stop_host_runner_intentionally(
                parent.id, "host", runner_id, None, store
            )
        )
        if outcome == "rejected_while_running":
            await asyncio.wait_for(dispatched.wait(), timeout=_TASK_TIMEOUT_S)
            yield 'data: {"type": "session.status", "status": "running"}\n\n'
            acknowledgement.set()
        acknowledged = await asyncio.wait_for(stop_task, timeout=_TASK_TIMEOUT_S)
        assert acknowledged is (outcome == "acknowledged")
        if outcome != "rejected_while_running":
            assert sessions_module._intentional_stop_sessions.get(session_id) == runner_id
            yield 'data: {"type": "session.status", "status": "running"}\n\n'
        raise ConnectionError("intentional runner teardown")

    monkeypatch.setattr(response, "aiter_text", stream_events)
    monkeypatch.setattr(runner, "stream", lambda *_args, **_kwargs: response)
    collector = None
    handle = None
    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            runner_id,
            runner,  # type: ignore[arg-type]
            conversation_store=store,
        )
        assert handle is not None
        collector = await start_session_stream_collector(session_id)
        gate.set()
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        statuses = []
        while not collector.queue.empty():
            statuses.append(collector.queue.get_nowait())
        persisted = store.get_conversation(session_id)
        assert persisted is not None
        error = sessions_module._last_task_error_from_labels(persisted.labels)
        failed = [event for event in statuses if event.get("status") == "failed"]
        if outcome.startswith("rejected"):
            assert failed and failed[-1]["error"]["code"] == "runner_disconnected", statuses
            assert sessions_module._session_status_cache[session_id] == "failed"
            assert error is not None and error["code"] == "runner_disconnected"
        else:
            assert not failed, statuses
            assert any(event.get("status") == "idle" for event in statuses), statuses
            assert sessions_module._session_status_cache[session_id] == "idle"
            assert error is None
    finally:
        gate.set()
        acknowledgement.set()
        if collector is not None:
            await collector.stop()
        if handle is not None:
            handle.task.cancel()
            await asyncio.gather(handle.task, return_exceptions=True)
        if stop_task is not None:
            stop_task.cancel()
            await asyncio.gather(stop_task, return_exceptions=True)
        sessions_module._runner_relay_tasks.pop(session_id, None)
        for row in (parent, child):
            sessions_module._intentional_stop_sessions.pop(row.id, None)
            sessions_module._session_status_cache.pop(row.id, None)
            session_stream.close(row.id)


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
        sessions_module._intentional_stop_sessions[session_id] = "runner_stale_marker"

        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_stale_marker",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None

        collector = await start_session_stream_collector(session_id)
        gate.set()
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

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
        sessions_module._intentional_stop_sessions.pop(session_id, None)
        if collector is not None:
            await collector.stop()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("prior_error_code", "expect_cleared"),
    [
        ("runner_disconnected", True),
        ("agent_error", False),
    ],
)
async def test_relay_completion_idle_clears_only_a_disconnect_failure(
    monkeypatch: pytest.MonkeyPatch,
    prior_error_code: str,
    expect_cleared: bool,
) -> None:
    """
    The runner's completion ``idle`` clears a stale ``runner_disconnected`` failure.

    A false disconnect fail (the tunnel dropped, but the runner kept working
    and reconnected) lands the cache on ``failed``. When the runner then
    finishes the turn its ``idle`` edge must be honored and the disconnect
    labels cleared; otherwise the sticky-failed rule swallows the completion
    and the red card stays until the next user message. A genuine task
    failure with any other code must stay sticky, exactly as before.
    """
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S",
        0.0,
    )
    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    frames = ['data: {"type": "session.status", "status": "idle"}\n\n']
    fake_runner = _ScriptedThenDropRunnerClient(frames, gate)
    store = _RecordingLabelStore(live_status="idle")
    session_id = "d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6"
    store.set_labels(
        session_id,
        {
            sessions_module._LAST_TASK_ERROR_CODE_LABEL_KEY: prior_error_code,
            sessions_module._LAST_TASK_ERROR_MESSAGE_LABEL_KEY: "prior failure",
        },
    )
    sessions_module._session_status_cache[session_id] = "failed"

    collector = None
    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_completion_idle",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None
        collector = await start_session_stream_collector(session_id)
        gate.set()
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

        statuses = []
        while not collector.queue.empty():
            statuses.append(await collector.queue.get())
        idle_edges = [e for e in statuses if e.get("status") == "idle"]
        persisted = sessions_module._last_task_error_from_labels(store.labels[session_id])
        if expect_cleared:
            assert sessions_module._session_status_cache.get(session_id) == "idle", (
                f"completion idle was swallowed by the sticky-failed rule; saw {statuses}"
            )
            assert idle_edges, f"no idle edge reached the stream; saw {statuses}"
            assert persisted is None, f"disconnect labels survived recovery: {persisted}"
        else:
            assert sessions_module._session_status_cache.get(session_id) == "failed", (
                f"a genuine {prior_error_code} failure was downgraded to idle; saw {statuses}"
            )
            assert persisted is not None and persisted["code"] == prior_error_code
    finally:
        gate.set()
        if collector is not None:
            await collector.stop()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


class _RegisteredRunnerHttpErrorClient:
    """Fake runner client whose transport says the runner is present but every stream open fails.

    Models a connected tunnel whose ``GET /stream`` keeps returning an HTTP
    error: the relay must keep its interval backoff here, because the
    runner-absence waiter resolves immediately for a registered runner.
    """

    class _Transport:
        async def wait_for_runner(self, timeout_s: float) -> bool:
            del timeout_s
            return True

    def __init__(self, gate: asyncio.Event) -> None:
        self.calls = 0
        self._gate = gate
        self._transport = self._Transport()

    def stream(self, method: str, path: str, *, timeout: Any) -> Any:
        del method, path, timeout
        self.calls += 1
        if self.calls == 1:
            # First open: heartbeat so the relay reports ready, then drop.
            return _ScriptedThenDropStreamResponse([], self._gate)
        request = httpx.Request("GET", "http://runner/v1/sessions/x/stream")
        response = httpx.Response(503, request=request)
        raise httpx.HTTPStatusError("503", request=request, response=response)


@pytest.mark.asyncio
async def test_relay_backs_off_when_a_registered_runner_rejects_the_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A registered runner that rejects the stream gets interval retries, not a storm.

    The relay parks on runner re-registration during an outage. For a runner
    that is still registered, that wait returns at once, so an HTTP error from
    a healthy tunnel must fall back to the interval sleep or the relay would
    re-open the stream as fast as the loop turns.
    """
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S",
        0.5,
    )
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration._RELAY_RETRY_INTERVAL_S",
        0.1,
    )
    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    fake_runner = _RegisteredRunnerHttpErrorClient(gate)
    store = _RecordingLabelStore(live_status="idle")
    session_id = "e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0"
    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_http_error_storm",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None
        gate.set()
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        # 0.5s grace / 0.1s interval bounds the attempts to a handful; a
        # storm makes hundreds.
        assert fake_runner.calls <= 8, (
            f"relay re-opened the stream {fake_runner.calls} times against a registered "
            "runner within a 0.5s grace: the runner-absence wait is short-circuiting the backoff"
        )
    finally:
        gate.set()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)


@pytest.mark.asyncio
async def test_relay_survives_a_failed_recovery_read_and_keeps_delivering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A database error while clearing a disconnect failure must not kill the relay.

    The completion-idle recovery reads the conversation row to confirm the
    failure is a ``runner_disconnected``. The relay supervisor only handles
    transport loss, so an unguarded read error would end the relay task and
    silently drop every later runner event for the session. The recovery must
    fail soft: keep the existing ``failed`` status and keep streaming.
    """
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S",
        0.0,
    )
    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    # Completion idle (triggers the recovery read), then a new turn's running edge.
    frames = [
        'data: {"type": "session.status", "status": "idle"}\n\n',
        'data: {"type": "session.status", "status": "running"}\n\n',
    ]
    fake_runner = _ScriptedThenDropRunnerClient(frames, gate)
    store = _RecordingLabelStore(live_status="idle")
    session_id = "f1e2d3c4b5a6978877665544332211aa"
    store.set_labels(
        session_id,
        {
            sessions_module._LAST_TASK_ERROR_CODE_LABEL_KEY: "runner_disconnected",
            sessions_module._LAST_TASK_ERROR_MESSAGE_LABEL_KEY: "prior disconnect",
        },
    )
    sessions_module._session_status_cache[session_id] = "failed"
    monkeypatch.setattr(
        store,
        "get_conversation",
        lambda conversation_id: (_ for _ in ()).throw(RuntimeError("db blip")),
    )

    collector = None
    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_recovery_read_fails",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None
        collector = await start_session_stream_collector(session_id)
        gate.set()
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

        statuses = []
        while not collector.queue.empty():
            statuses.append(await collector.queue.get())
        running = [e for e in statuses if e.get("status") == "running"]
        assert running, (
            "the running edge after the failed recovery read never reached the stream: "
            f"the relay died on the read error; saw {statuses}"
        )
    finally:
        gate.set()
        if collector is not None:
            await collector.stop()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_relay_stays_quiet_when_runner_leaves_an_idle_session(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
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
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

        # Wait for each edge rather than draining a snapshot: the quiet path
        # publishes nothing and awaits nothing, so the relay task can finish
        # before the collector's pump is ever scheduled.
        statuses: list[dict[str, Any]] = []
        while len([e for e in statuses if e.get("type") == "session.status"]) < 2:
            statuses.append(await asyncio.wait_for(collector.queue.get(), timeout=_TASK_TIMEOUT_S))
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
        record = next(
            r
            for r in caplog.records
            if getattr(r, "event_name", None) == "runner_stream_disconnected"
        )
        assert record.session_id == session_id
        assert record.attributes["intentional_stop"] is False
        assert record.attributes["cached_session_status"] == "idle"
        assert record.attributes["decision"] == "idle_no_failure"
    finally:
        gate.set()
        if collector is not None:
            await collector.stop()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
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
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

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
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "adopted_status", "later_lookup", "live_status", "decision", "status_source"),
    [
        ("sub_agent", "idle", "missing", None, "idle_no_failure", "relay_snapshot"),
        ("sub_agent", "idle", "error", None, "idle_no_failure", "relay_snapshot"),
        ("sub_agent", "running", "error", None, "failed_mid_turn", "relay_snapshot"),
        ("sub_agent", "waiting", "missing", None, "failed_mid_turn", "relay_snapshot"),
        ("sub_agent", None, "missing", None, "failed_mid_turn", "unknown"),
        ("sub_agent", "idle", "running", None, "failed_mid_turn", "persisted"),
        ("sub_agent", "idle", "waiting", None, "failed_mid_turn", "persisted"),
        ("sub_agent", "running", "idle", None, "idle_no_failure", "persisted"),
        ("sub_agent", "idle", "idle", "running", "failed_mid_turn", "cache"),
        ("sub_agent", "running", "running", "idle", "idle_no_failure", "cache"),
        # A native mirror's saved status can read mid-turn after its last idle
        # edge, and its parent's runtime owns the turn: only the cache fails it.
        ("mirror", "running", "error", None, "subagent_unobserved", "relay_snapshot"),
        ("mirror", "waiting", "missing", None, "subagent_unobserved", "relay_snapshot"),
        ("mirror", None, "missing", None, "failed_mid_turn", "unknown"),
        ("mirror", "idle", "running", None, "subagent_unobserved", "persisted"),
        ("mirror", "idle", "idle", "running", "failed_mid_turn", "cache"),
        # A top-level session's saved mid-turn status still reports the drop.
        ("default", "running", "error", None, "failed_mid_turn", "relay_snapshot"),
        ("default", "idle", "running", None, "failed_mid_turn", "persisted"),
    ],
)
async def test_relay_disconnect_status_after_adoption(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    adopted_status: str | None,
    later_lookup: str,
    live_status: str | None,
    decision: str,
    status_source: str,
) -> None:
    """Saved adoption state is a fallback; newer state still decides interruptions."""
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S", 0.0
    )
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    # ``default`` binds an unrelated top-level session the same way.
    child = store.create_conversation(
        kind="default" if kind == "default" else "sub_agent",
        parent_conversation_id=None if kind == "default" else parent.id,
        runner_id="runner_adopted",
    )
    if kind == "mirror":
        store.set_labels(child.id, {"omnigent.wrapper": "claude-code-native-ui-subagent"})
    if adopted_status is not None:
        store.set_session_live_status(child.id, adopted_status)
    snapshot = store.get_conversation(child.id)
    assert snapshot is not None
    gate = asyncio.Event()
    frames = (
        [f'data: {{"type": "session.status", "status": "{live_status}"}}\n\n']
        if live_status is not None
        else []
    )
    runner = _ScriptedThenDropRunnerClient(frames, gate)
    get_conversation = store.get_conversation
    lookup_attempted = False

    def disconnect_lookup(conversation_id: str) -> Conversation | None:
        nonlocal lookup_attempted
        if conversation_id == child.id and not lookup_attempted:
            # Fail only the disconnect read so genuine failures can still fan out.
            lookup_attempted = True
            if later_lookup == "error":
                raise RuntimeError("status lookup unavailable")
            if later_lookup == "missing":
                return None
        return get_conversation(conversation_id)

    handle = None
    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            child.id,
            child.runner_id,
            runner,  # type: ignore[arg-type]
            store,
            conversation=snapshot,
        )
        assert handle is not None
        assert sessions_module._session_status_cache.get(child.id) is None
        if later_lookup not in {"missing", "error"}:
            store.set_session_live_status(child.id, later_lookup)
        monkeypatch.setattr(store, "get_conversation", disconnect_lookup)

        with capture_debug_rows("server") as rows:
            gate.set()
            await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

        refreshed = get_conversation(child.id)
        assert refreshed is not None
        error = sessions_module._last_task_error_from_labels(refreshed.labels)
        if decision == "failed_mid_turn":
            assert sessions_module._session_status_cache[child.id] == "failed"
            assert error is not None and error["code"] == "runner_disconnected"
            if kind != "default":
                items = store.list_items(parent.id).data
                assert len(items) == 1 and items[0].data.resource["status"] == "failed"
        else:
            assert sessions_module._session_status_cache.get(child.id) != "failed"
            assert error is None
            assert store.list_items(parent.id).data == []
            assert not any(row["event_name"] == "session_turn_failed" for row in rows)
            if decision == "subagent_unobserved":
                assert refreshed.live_status in {"running", "waiting"}
        logged = next(row for row in rows if row["event_name"] == "runner_disconnect_decision")
        assert logged["attributes"]["status_source"] == status_source
        assert logged["attributes"]["decision"] == decision
        if adopted_status is not None:
            assert logged["attributes"]["snapshot_session_status"] == adopted_status
    finally:
        gate.set()
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await handle.task
        sessions_module._session_status_cache.pop(child.id, None)
        session_stream.close(child.id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario", "expect_failed"),
    [
        ("wrong_session", True),
        ("wrong_runner", True),
        ("rebind", True),
        ("caller_mutation", False),
        ("healthy_reuse", False),
    ],
)
async def test_relay_adoption_snapshot_lifetime(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
    expect_failed: bool,
) -> None:
    """The fallback belongs to one relay binding, independent of caller mutations."""
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S", 0.0
    )
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(runner_id="runner_original")
    store.set_session_live_status(conv.id, "idle")
    snapshot = store.get_conversation(conv.id)
    assert snapshot is not None
    if scenario == "wrong_session":
        snapshot.id = "another_session"
    elif scenario == "wrong_runner":
        snapshot.runner_id = "another_runner"
    gate = asyncio.Event()
    runner = _TunnelCloseRunnerClient(gate)
    get_conversation = store.get_conversation
    lookup_attempted = False

    def missing_disconnect_lookup(conversation_id: str) -> Conversation | None:
        nonlocal lookup_attempted
        if conversation_id == conv.id and not lookup_attempted:
            lookup_attempted = True
            return None
        return get_conversation(conversation_id)

    handle = None
    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            conv.id,
            conv.runner_id,
            runner,  # type: ignore[arg-type]
            store,
            conversation=snapshot,
        )
        assert handle is not None
        if scenario == "rebind":
            original = handle
            store.replace_runner_id(conv.id, "runner_replacement")
            handle = await sessions_module._ensure_runner_relay_ready(
                conv.id,
                "runner_replacement",
                runner,  # type: ignore[arg-type]
                store,
                conversation=snapshot,
            )
            assert handle is not None and handle is not original
            with contextlib.suppress(asyncio.CancelledError):
                await original.task
        elif scenario in {"caller_mutation", "healthy_reuse"}:
            snapshot.live_status = "running"
            if scenario == "healthy_reuse":
                reused = await sessions_module._ensure_runner_relay_ready(
                    conv.id,
                    conv.runner_id,
                    runner,  # type: ignore[arg-type]
                    store,
                    conversation=snapshot,
                )
                assert reused is handle
        monkeypatch.setattr(store, "get_conversation", missing_disconnect_lookup)
        gate.set()
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        assert lookup_attempted
        assert (sessions_module._session_status_cache.get(conv.id) == "failed") == expect_failed
    finally:
        gate.set()
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await handle.task
        sessions_module._session_status_cache.pop(conv.id, None)
        session_stream.close(conv.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_path", ["relay", "sweep"])
@pytest.mark.parametrize("lookup_result", ["found", "missing", "error"])
@pytest.mark.parametrize(
    ("persisted_status", "arriving_status", "expect_failed"),
    [("running", "idle", False), ("idle", "running", True), ("idle", "waiting", True)],
)
async def test_disconnect_uses_status_arriving_during_lookup(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    failure_path: str,
    lookup_result: str,
    persisted_status: str,
    arriving_status: str,
    expect_failed: bool,
) -> None:
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.schemas import ErrorDetail

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S", 0.0
    )
    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(kind="sub_agent", parent_conversation_id=parent.id)
    store.set_session_live_status(child.id, persisted_status)
    snapshot = store.get_conversation(child.id)
    assert snapshot is not None
    read_started = asyncio.Event()
    release_read = threading.Event()
    loop = asyncio.get_running_loop()
    get_conversation = store.get_conversation

    def delayed_get(conversation_id: str) -> Conversation | None:
        conv = get_conversation(conversation_id)
        if conversation_id == child.id and not release_read.is_set():
            # Hold the real DB snapshot while a newer lifecycle edge arrives.
            loop.call_soon_threadsafe(read_started.set)
            assert release_read.wait(_TASK_TIMEOUT_S)
            if lookup_result == "error":
                raise RuntimeError("status lookup unavailable")
            if lookup_result == "missing":
                return None
        return conv

    monkeypatch.setattr(store, "get_conversation", delayed_get)
    gate = asyncio.Event()
    gate.set()
    # Only the runner transport is scripted; the relay and persistence are real.
    runner = _ScriptedThenDropRunnerClient([], gate)
    origin = "runner_disconnected_mid_turn" if failure_path == "relay" else "runner_offline_sweep"
    task = None
    try:
        with capture_debug_rows("server") as rows:
            task = asyncio.create_task(
                sessions_module._relay_runner_stream(
                    child.id,
                    runner,  # type: ignore[arg-type]
                    store,
                )
                if failure_path == "relay"
                else sessions_module._mark_runner_sessions_offline(
                    [snapshot],
                    ErrorDetail(code="runner_disconnected", message="Disconnected"),
                    store,
                )
            )
            await asyncio.wait_for(read_started.wait(), timeout=_TASK_TIMEOUT_S)
            sessions_module._publish_status(child.id, arriving_status)
            release_read.set()
            await asyncio.wait_for(task, timeout=_TASK_TIMEOUT_S)

        expected = "failed" if expect_failed else arriving_status
        assert sessions_module._session_status_cache[child.id] == expected
        refreshed = get_conversation(child.id)
        assert refreshed is not None
        error = sessions_module._last_task_error_from_labels(refreshed.labels)
        if expect_failed:
            assert error is not None and error["code"] == "runner_disconnected"
            items = store.list_items(parent.id).data
            assert len(items) == 1
            assert items[0].data.resource["status"] == "failed"
        else:
            assert error is None
            assert store.list_items(parent.id).data == []
            assert not any(row["event_name"] == "session_turn_failed" for row in rows)

        decision = next(row for row in rows if row["event_name"] == "runner_disconnect_decision")
        assert decision["level"] == "WARNING"
        assert (
            decision["attributes"].items()
            >= {
                "origin": origin,
                "decision": "failed_mid_turn" if expect_failed else "idle_no_failure",
                "status_source": "cache",
                "cached_session_status": arriving_status,
                "status_lookup": lookup_result,
            }.items()
        )
        if lookup_result == "found":
            assert decision["attributes"]["persisted_session_status"] == persisted_status
            assert decision["attributes"]["parent_session_id"] == parent.id
            assert decision["attributes"]["session_kind"] == "sub_agent"
    finally:
        release_read.set()
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        sessions_module._session_status_cache.pop(child.id, None)
        session_stream.close(child.id)


@pytest.mark.asyncio
@pytest.mark.parametrize("missing_row", [False, True])
async def test_relay_reports_the_drop_when_live_status_is_unknown(
    monkeypatch: pytest.MonkeyPatch,
    missing_row: bool,
) -> None:
    """
    An unreadable or missing row still reports the drop.

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

    def unavailable_conversation(conversation_id: str) -> None:
        if not missing_row:
            raise RuntimeError("db blip")

    monkeypatch.setattr(store, "get_conversation", unavailable_conversation)
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
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

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
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


class _FlakyThenHealthyRunnerClient:
    """Fake runner client that drops once, then serves a clean stream.

    The first ``stream`` call raises the ``ConnectionError`` shape
    ``WSTunnelTransport`` emits while the runner is deregistered; later
    calls serve a heartbeat and a terminating ``[DONE]``.
    """

    def __init__(self) -> None:
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
        if self.calls == 1:
            raise ConnectionError("tunnel closed before request completed")
        release = asyncio.Event()
        release.set()
        return _HeartbeatStreamResponse(release)


class _RepeatedRecoveryRunnerClient:
    """Drop before readiness, recover, drop after readiness, then recover again."""

    def __init__(self) -> None:
        self.calls = 0
        self.release = asyncio.Event()
        self.release.set()

    def stream(
        self,
        method: str,
        path: str,
        *,
        timeout: Any,
    ) -> _HeartbeatStreamResponse:
        del method, path, timeout
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("tunnel closed before request completed")
        return _HeartbeatStreamResponse(self.release, drop=self.calls == 2)


class _NeverReadyStreamResponse:
    """SSE response that never yields the readiness heartbeat."""

    def __init__(self, release: asyncio.Event) -> None:
        self._release = release

    async def __aenter__(self) -> _NeverReadyStreamResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    def raise_for_status(self) -> None:
        """The scripted stream represents a successful HTTP response."""

    async def aiter_text(self) -> AsyncIterator[str]:
        await self._release.wait()
        if False:
            yield ""


class _DropThenNeverReadyRunnerClient:
    """Drop once, then wait before readiness until the relay is cancelled."""

    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

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
        self.started.set()
        return _NeverReadyStreamResponse(self.release)


class _DelayedNeverReadyStreamResponse:
    """SSE response that spends longer than grace before dropping."""

    def __init__(self, delay_s: float) -> None:
        self._delay_s = delay_s

    async def __aenter__(self) -> _DelayedNeverReadyStreamResponse:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback

    def raise_for_status(self) -> None:
        """The scripted stream represents a successful HTTP response."""

    async def aiter_text(self) -> AsyncIterator[str]:
        await asyncio.sleep(self._delay_s)
        raise ConnectionError("tunnel closed before request completed")
        if False:
            yield ""


class _LongNoReadyAttemptRunnerClient:
    """A no-ready attempt exceeds grace before a later ready recovery."""

    def __init__(self, delay_s: float) -> None:
        self.calls = 0
        self._delay_s = delay_s

    def stream(
        self,
        method: str,
        path: str,
        *,
        timeout: Any,
    ) -> _HeartbeatStreamResponse | _DelayedNeverReadyStreamResponse:
        del method, path, timeout
        self.calls += 1
        if self.calls == 1:
            raise ConnectionError("tunnel closed before request completed")
        if self.calls == 2:
            return _DelayedNeverReadyStreamResponse(self._delay_s)
        release = asyncio.Event()
        release.set()
        return _HeartbeatStreamResponse(release)


@pytest.mark.asyncio
async def test_relay_retries_transport_drop_within_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A transport drop inside the grace reconnects without failing the session.

    Transient tunnel drops (ingress recycles, sleep-wake reconnects)
    re-register the runner well inside the grace, so the relay must retry
    its stream instead of publishing ``failed``/``runner_disconnected``
    for a blip the next attempt rides out.
    """
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration._RELAY_RETRY_INTERVAL_S",
        0.01,
    )
    sessions_module._runner_relay_tasks.clear()
    fake_runner = _FlakyThenHealthyRunnerClient()
    store = _RecordingLabelStore()
    session_id = "5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d"

    try:
        with capture_debug_rows("server") as rows:
            handle = await sessions_module._ensure_runner_relay_ready(
                session_id,
                "runner_flaky_then_healthy",
                fake_runner,  # type: ignore[arg-type]
                conversation_store=store,  # type: ignore[arg-type]
            )
            assert handle is not None
            await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

        assert fake_runner.calls == 2, "relay did not retry after the drop"
        # The blip resolved silently: no failed status reached the cache
        # and no runner_disconnected labels were persisted.
        assert sessions_module._session_status_cache.get(session_id) is None
        assert store.labels.get(session_id) is None
        # It is still recorded: one outage-start row naming the grace the turn
        # was held for, and no give-up row since the retry rode it out.
        from omnigent.server.routes._sessions.orchestration import RUNNER_DISCONNECT_GRACE_S

        events = [row["event_name"] for row in rows]
        assert events.count("runner_stream_transport_lost") == 1
        assert events.count("runner_stream_recovered") == 1
        assert "runner_stream_disconnected" not in events
        lost = next(row for row in rows if row["event_name"] == "runner_stream_transport_lost")
        recovered = next(row for row in rows if row["event_name"] == "runner_stream_recovered")
        assert lost["session_id"] == session_id
        assert lost["turn_id"] is None
        assert lost["attributes"]["runner_id"] == "runner_flaky_then_healthy"
        assert lost["attributes"]["stream_ready"] == "False"
        assert lost["attributes"]["grace_s"] == str(RUNNER_DISCONNECT_GRACE_S)
        assert lost["attributes"]["telemetry_schema"] == "runner_stream_recovery.v1"
        assert recovered["session_id"] == session_id
        assert recovered["turn_id"] is None
        assert recovered["attributes"]["outage_id"] == lost["attributes"]["outage_id"]
        assert recovered["attributes"]["runner_id"] == "runner_flaky_then_healthy"
        assert recovered["attributes"]["recovery_attempt"] == "1"
        assert recovered["attributes"]["recovery_evidence"] == "stream_heartbeat"
        assert recovered["attributes"]["telemetry_schema"] == "runner_stream_recovery.v1"
        assert float(recovered["attributes"]["outage_s"]) >= 0
    finally:
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)


@pytest.mark.asyncio
async def test_relay_recovery_rows_get_fresh_ids_for_repeated_ready_drop_cycles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Each ready-confirmed outage gets one distinct, serializer-visible ID."""
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration._RELAY_RETRY_INTERVAL_S", 0.0
    )
    sessions_module._runner_relay_tasks.clear()
    runner = _RepeatedRecoveryRunnerClient()
    store = _RecordingLabelStore()
    session_id = "6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e"
    sessions_module._session_active_response_cache[session_id] = "turn-loss"

    try:
        with capture_debug_rows("server") as rows:
            await asyncio.wait_for(
                sessions_module._relay_runner_stream(
                    session_id,
                    runner,  # type: ignore[arg-type]
                    store,  # type: ignore[arg-type]
                    runner_id="runner_repeated_recovery",
                ),
                timeout=_TASK_TIMEOUT_S,
            )

        losses = [row for row in rows if row["event_name"] == "runner_stream_transport_lost"]
        recoveries = [row for row in rows if row["event_name"] == "runner_stream_recovered"]
        assert runner.calls == 3
        assert len(losses) == len(recoveries) == 2
        loss_ids = {row["attributes"]["outage_id"] for row in losses}
        recovery_ids = {row["attributes"]["outage_id"] for row in recoveries}
        assert len(loss_ids) == 2
        assert recovery_ids == loss_ids
        assert {row["attributes"]["stream_ready"] for row in losses} == {"False", "True"}
        assert {row["turn_id"] for row in losses} == {"turn-loss"}
        assert {row["turn_id"] for row in recoveries} == {"turn-loss"}
        assert all(
            row["attributes"]["telemetry_schema"] == "runner_stream_recovery.v1"
            for row in losses + recoveries
        )
        assert all(float(row["attributes"]["outage_s"]) >= 0 for row in recoveries)
        assert not any(row["event_name"] == "runner_stream_disconnected" for row in rows)
    finally:
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_active_response_cache.pop(session_id, None)


@pytest.mark.asyncio
async def test_relay_cancellation_before_ready_emits_no_recovery_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cancelled/rebound retry before heartbeat leaves its outage censored."""
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration._RELAY_RETRY_INTERVAL_S", 0.0
    )
    sessions_module._runner_relay_tasks.clear()
    runner = _DropThenNeverReadyRunnerClient()
    session_id = "7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f"
    task: asyncio.Task[None] | None = None

    try:
        with capture_debug_rows("server") as rows:
            task = asyncio.create_task(
                sessions_module._relay_runner_stream(
                    session_id,
                    runner,  # type: ignore[arg-type]
                    _RecordingLabelStore(),
                    runner_id="runner_cancel_before_ready",
                )
            )
            await asyncio.wait_for(runner.started.wait(), timeout=_TASK_TIMEOUT_S)
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        assert any(row["event_name"] == "runner_stream_transport_lost" for row in rows)
        assert not any(row["event_name"] == "runner_stream_recovered" for row in rows)
    finally:
        runner.release.set()
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        sessions_module._runner_relay_tasks.clear()


@pytest.mark.asyncio
async def test_relay_giveup_matches_loss_outage_and_turn_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A give-up row reuses the exact loss ID and loss-time turn identity."""
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S", 0.0
    )
    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    gate.set()
    runner = _TunnelCloseRunnerClient(gate)
    store = _RecordingLabelStore()
    session_id = "8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f30"
    sessions_module._session_active_response_cache[session_id] = "turn-giveup"

    try:
        with capture_debug_rows("server") as rows:
            await asyncio.wait_for(
                sessions_module._relay_runner_stream(
                    session_id,
                    runner,  # type: ignore[arg-type]
                    store,  # type: ignore[arg-type]
                    runner_id="runner_giveup_identity",
                ),
                timeout=_TASK_TIMEOUT_S,
            )

        lost = next(row for row in rows if row["event_name"] == "runner_stream_transport_lost")
        giveup = next(row for row in rows if row["event_name"] == "runner_stream_disconnected")
        assert lost["turn_id"] == "turn-giveup"
        assert giveup["turn_id"] == "turn-giveup"
        assert giveup["attributes"]["outage_id"] == lost["attributes"]["outage_id"]
        assert giveup["attributes"]["runner_id"] == "runner_giveup_identity"
        assert giveup["attributes"]["telemetry_schema"] == "runner_stream_recovery.v1"
        assert not any(row["event_name"] == "runner_stream_recovered" for row in rows)
    finally:
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_active_response_cache.pop(session_id, None)


@pytest.mark.asyncio
async def test_relay_long_unready_attempt_starts_a_new_outage_without_false_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A grace reset without heartbeat does not recover the prior outage."""
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S", 0.01
    )
    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration._RELAY_RETRY_INTERVAL_S", 0.0
    )
    sessions_module._runner_relay_tasks.clear()
    runner = _LongNoReadyAttemptRunnerClient(delay_s=0.05)
    session_id = "9e0f1a2b3c4d5e6f7a8b9c0d1e2f3041"

    try:
        with capture_debug_rows("server") as rows:
            await asyncio.wait_for(
                sessions_module._relay_runner_stream(
                    session_id,
                    runner,  # type: ignore[arg-type]
                    _RecordingLabelStore(),
                    runner_id="runner_long_unready",
                ),
                timeout=_TASK_TIMEOUT_S,
            )

        losses = [row for row in rows if row["event_name"] == "runner_stream_transport_lost"]
        recoveries = [row for row in rows if row["event_name"] == "runner_stream_recovered"]
        assert runner.calls == 3
        assert len(losses) == 2
        assert len(recoveries) == 1
        assert all(row["attributes"]["stream_ready"] == "False" for row in losses)
        assert recoveries[0]["attributes"]["outage_id"] == losses[1]["attributes"]["outage_id"]
        assert recoveries[0]["attributes"]["outage_id"] != losses[0]["attributes"]["outage_id"]
    finally:
        sessions_module._runner_relay_tasks.clear()


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
        ("sub_agent", None, "running", False, False, True),
        ("sub_agent", None, "waiting", False, False, True),
        ("sub_agent", None, "idle", False, False, False),
        ("default", None, "idle", False, False, False),
        ("default", None, None, False, False, False),
        # Local turn edges can be ahead of asynchronous persistence.
        ("sub_agent", "idle", "running", False, False, False),
        ("sub_agent", "running", "idle", False, False, True),
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
    db_uri: str,
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

    store = SqlAlchemyConversationStore(db_uri)
    parent_id = store.create_conversation().id if kind == "sub_agent" else None
    conv = store.create_conversation(
        kind=kind, parent_conversation_id=parent_id, runner_id="runner-offline"
    )
    session_id = conv.id
    if live_status is not None:
        store.set_session_live_status(session_id, live_status)
    snapshot = store.get_conversation(session_id)
    assert snapshot is not None
    error = ErrorDetail(code="runner_disconnected", message="Runner disconnected unexpectedly.")
    if cached is not None:
        sessions_module._session_status_cache[session_id] = cached
    if intentional_stop:
        sessions_module._intentional_stop_sessions[session_id] = "runner-offline"

    try:
        await sessions_module._mark_runner_sessions_offline(
            [snapshot],
            error,
            store,
            fail_idle_top_level=fail_idle_top_level,
        )

        status = sessions_module._session_status_cache.get(session_id)
        refreshed = store.get_conversation(session_id)
        assert refreshed is not None
        persisted = sessions_module._last_task_error_from_labels(refreshed.labels)
        if expect_failed:
            assert status == "failed"
            # The cause must be durable: it is what lets the UI render a
            # benign "Disconnected" and what
            # ``_publish_runner_recovered_status`` matches on to clear the
            # failure when the runner comes back.
            assert persisted == {
                "code": "runner_disconnected",
                "message": "Runner disconnected unexpectedly.",
            }
        elif intentional_stop and (cached or live_status) in {"running", "waiting"}:
            assert status == "idle"
            assert persisted is None
            assert session_id not in sessions_module._intentional_stop_sessions
        else:
            assert status == cached
            assert persisted is None
    finally:
        sessions_module._intentional_stop_sessions.pop(session_id, None)
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("labels", "fail_idle_top_level", "decision"),
    [
        ({"omnigent.wrapper": "claude-code-native-ui-subagent"}, False, "subagent_unobserved"),
        ({"omnigent.wrapper": "claude-code-native-ui-subagent"}, True, "subagent_unobserved"),
        ({"omnigent.wrapper": "codex-native-ui-subagent"}, False, "subagent_unobserved"),
        ({"omnigent.acp.subagent_id": "acp_sub_1"}, False, "subagent_unobserved"),
        # A sys_session_create child's failure label drives the runner's
        # restart recovery, so its saved status still decides.
        ({}, False, "failed_mid_turn"),
    ],
)
async def test_offline_sweep_saved_subagent_turn_without_a_cached_edge(
    db_uri: str,
    labels: dict[str, str],
    fail_idle_top_level: bool,
    decision: str,
) -> None:
    """
    A saved running status alone does not fail a native parent's sub-agent mirror.

    A mirror can still read ``running`` after its last idle edge, so failing
    on it painted finished children red. The parent's runtime owns the turn
    and its result; reconnect re-attaches the mirror, which still counts as
    interrupted.
    """
    from omnigent.runtime import session_stream
    from omnigent.server.child_session_recovery import _interrupted
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.server.schemas import ErrorDetail

    store = SqlAlchemyConversationStore(db_uri)
    parent = store.create_conversation()
    child = store.create_conversation(kind="sub_agent", parent_conversation_id=parent.id)
    if labels:
        store.set_labels(child.id, labels)
    store.set_session_live_status(child.id, "running")
    snapshot = store.get_conversation(child.id)
    assert snapshot is not None
    error = ErrorDetail(code="runner_disconnected", message="Runner disconnected unexpectedly.")

    try:
        with capture_debug_rows("server") as rows:
            await sessions_module._mark_runner_sessions_offline(
                [snapshot], error, store, fail_idle_top_level=fail_idle_top_level
            )

        refreshed = store.get_conversation(child.id)
        assert refreshed is not None
        persisted = sessions_module._last_task_error_from_labels(refreshed.labels)
        if decision == "subagent_unobserved":
            assert refreshed.live_status == "running"
            assert persisted is None
            assert store.list_items(parent.id).data == []
            assert _interrupted(refreshed)
        else:
            assert persisted is not None and persisted["code"] == "runner_disconnected"
        logged = next(row for row in rows if row["event_name"] == "runner_disconnect_decision")
        assert logged["attributes"]["origin"] == "runner_offline_sweep"
        assert logged["attributes"]["decision"] == decision
        assert logged["attributes"]["status_source"] == "persisted"
        assert logged["attributes"]["session_kind"] == "sub_agent"
    finally:
        sessions_module._session_status_cache.pop(child.id, None)
        session_stream.close(child.id)


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
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

        assert session_id not in store.labels, "shutdown-time drop persisted failure labels"
        assert sessions_module._session_status_cache.get(session_id) == "running"
    finally:
        shutdown_state.reset_for_tests()
        gate.set()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_relay_reads_handoff_evidence_with_conversation_database_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A conversation-engine outage must not hide a healthy metadata heartbeat."""
    import time

    from sqlalchemy import event

    from omnigent.server.routes._sessions.orchestration import _relay_runner_live_elsewhere

    store = SqlAlchemyConversationStore(
        f"sqlite:///{tmp_path / 'metadata.db'}",
        f"sqlite:///{tmp_path / 'conversations.db'}",
    )
    runner_id = "runner_handed_off"
    conversation = store.create_conversation(runner_id=runner_id)
    now = int(time.time())
    store.touch_runner_liveness([runner_id], now)
    monkeypatch.setattr(
        "omnigent.server.session_live_state.last_liveness_stamp",
        lambda _runner_id: now - 1,
    )

    def unavailable(*_args: Any) -> None:
        raise ConnectionError("conversation database unavailable")

    event.listen(store._conv_engine, "before_cursor_execute", unavailable)
    try:
        with pytest.raises(ConnectionError, match="conversation database unavailable"):
            store.get_session_connectivity([conversation.id])
        assert await asyncio.wait_for(
            _relay_runner_live_elsewhere(conversation.id, store), timeout=_TASK_TIMEOUT_S
        )
    finally:
        event.remove(store._conv_engine, "before_cursor_execute", unavailable)


@pytest.mark.asyncio
@pytest.mark.parametrize("conversation_backend_unavailable", [False, True])
async def test_relay_stays_quiet_when_runner_is_live_on_another_replica(
    monkeypatch: pytest.MonkeyPatch,
    conversation_backend_unavailable: bool,
) -> None:
    """
    A runner already re-tunnelled to another replica is not failed here.

    The runner may reconnect elsewhere before this replica's grace expires
    (ingress recycle, a 4003 close after a silent stretch). That replica's
    fresh ``runner_last_seen`` stamp means it now owns the turn, so this
    drop must publish no ``failed`` status and persist no
    ``runner_disconnected`` labels — mirroring the idle-session case.
    """
    import time

    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S",
        0.0,
    )
    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    fake_runner = _TunnelCloseRunnerClient(gate)
    runner_id = "runner_live_elsewhere"
    session_id = "d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6"
    now = int(time.time())
    # This replica's own last stamp is a minute old; the row's fresh stamp can
    # only come from the replica the runner re-tunnelled to.
    monkeypatch.setattr(
        "omnigent.server.session_live_state.last_liveness_stamp",
        lambda _runner_id: now - 60,
    )
    store = _RecordingLabelStore(runner_liveness={session_id: (runner_id, now)})
    if conversation_backend_unavailable:

        def unavailable(conversation_id: str) -> Any:
            raise ConnectionError("conversation backend unavailable")

        monkeypatch.setattr(store, "get_conversation", unavailable)
    # A turn is in flight, so a plain disconnect (without the cross-replica
    # check) would otherwise fail it.
    sessions_module._session_status_cache[session_id] = "running"
    sessions_module._session_active_response_cache[session_id] = "response-live-elsewhere"

    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            runner_id,
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None
        gate.set()
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

        assert session_id not in store.labels, "drop persisted failure labels"
        assert sessions_module._session_status_cache.get(session_id) is None
        assert sessions_module._session_active_response_cache.get(session_id) is None
    finally:
        gate.set()
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        sessions_module._session_active_response_cache.pop(session_id, None)
        session_stream.close(session_id)


def test_runner_live_elsewhere_uses_preloaded_conversation_stamps() -> None:
    """The grace path can detect a newer replica from already-loaded rows."""
    import time
    from types import SimpleNamespace

    from omnigent.server.routes.sessions import (
        _runner_live_on_another_replica_from_conversations,
    )
    from omnigent.stores.conversation_store import RUNNER_LIVENESS_TTL_S

    now = int(time.time())
    expired_stamp = now - RUNNER_LIVENESS_TTL_S - 1
    conversations = [SimpleNamespace(runner_id="runner_a", runner_last_seen=now)]

    assert _runner_live_on_another_replica_from_conversations(conversations, "runner_a", now - 1)
    assert not _runner_live_on_another_replica_from_conversations(conversations, "runner_a", now)
    assert not _runner_live_on_another_replica_from_conversations(
        [SimpleNamespace(runner_id="runner_a", runner_last_seen=expired_stamp)],
        "runner_a",
        expired_stamp - 1,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "liveness_state",
    [
        "cleared",
        "expired",
        "same-stamp",
        "older-stamp",
        "different-runner",
        "missing",
        "unavailable",
    ],
)
async def test_relay_still_fails_mid_turn_session_without_handoff_evidence(
    monkeypatch: pytest.MonkeyPatch,
    liveness_state: str,
) -> None:
    """
    Only positive evidence for this runner suppresses a mid-turn failure.

    Only a fresh stamp strictly newer than this replica's own reference
    proves another replica took over. Missing, unreadable, or mismatched
    runner metadata must still report a possible interruption.
    """
    import time

    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module
    from omnigent.stores.conversation_store import RUNNER_LIVENESS_TTL_S

    monkeypatch.setattr(
        "omnigent.server.routes._sessions.orchestration.RUNNER_DISCONNECT_GRACE_S",
        0.0,
    )
    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    fake_runner = _TunnelCloseRunnerClient(gate)
    runner_id = "runner_stale_or_cleared_stamp"
    session_id = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
    now = int(time.time())
    expired_stamp = now - RUNNER_LIVENESS_TTL_S - 1
    # The expired stamp is newer than the reference, isolating the TTL check.
    reference_stamp = expired_stamp - 1 if liveness_state == "expired" else now - 1
    monkeypatch.setattr(
        "omnigent.server.session_live_state.last_liveness_stamp",
        lambda _runner_id: reference_stamp,
    )
    stamp = {
        "cleared": None,
        "expired": expired_stamp,
        "same-stamp": reference_stamp,
        "older-stamp": reference_stamp - 1,
    }.get(liveness_state, now)
    store = _RecordingLabelStore(
        runner_liveness={}
        if liveness_state == "missing"
        else {
            session_id: (
                "other-runner" if liveness_state == "different-runner" else runner_id,
                stamp,
            )
        }
    )
    if liveness_state == "unavailable":

        def unavailable(conversation_id: str) -> Any:
            raise ConnectionError("metadata backend unavailable")

        monkeypatch.setattr(store, "get_runner_liveness", unavailable)
    sessions_module._session_status_cache[session_id] = "running"

    try:
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            runner_id,
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None
        gate.set()
        await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)

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
                await asyncio.wait_for(handle.task, timeout=_TASK_TIMEOUT_S)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)
        session_stream.close(session_id)


@pytest.mark.asyncio
async def test_relay_persist_error_once_emits_debug_row() -> None:
    """_relay_persist_error_once logs an error_item_persisted debug row on success."""
    from unittest.mock import MagicMock

    from omnigent.entities.conversation import ConversationItem, ErrorData, NewConversationItem
    from omnigent.server.routes._sessions.helpers import _relay_persist_error_once

    # Minimal fake store: list_items returns nothing (no duplicate), append returns
    # a list with one ConversationItem so the function can complete.
    persisted_item = ConversationItem(
        id="item_test",
        type="error",
        status="completed",
        response_id="resp_test",
        created_at=1753900000,
        data=ErrorData(
            source="execution",
            code="pi_credentials_unresolved",
            message="credential warning; do not log this",
        ),
    )
    fake_store = MagicMock()
    fake_store.list_items.return_value = MagicMock(data=[])
    fake_store.append.return_value = [persisted_item]

    item = NewConversationItem(
        type="error",
        response_id="resp_test",
        data=ErrorData(
            source="execution",
            code="pi_credentials_unresolved",
            message="credential warning; do not log this",
        ),
    )

    with capture_debug_rows("server") as rows:
        result = await _relay_persist_error_once(fake_store, "conv_test", item)

    assert result == "persisted"
    persist_rows = [r for r in rows if r.get("event_name") == "error_item_persisted"]
    assert len(persist_rows) == 1
    row = persist_rows[0]
    assert row["session_id"] == "conv_test"
    assert row["attributes"]["code"] == "pi_credentials_unresolved"
    assert row["attributes"]["source"] == "execution"
    # level is None for a destructive error; it must not appear in attributes.
    assert "level" not in row["attributes"] or row["attributes"]["level"] is None
    # message text must never reach the debug table
    assert "credential warning" not in str(row)
    assert "do not log" not in str(row)


def test_runner_disconnect_grace_exceeds_runner_worst_case_reconnect() -> None:
    """The grace must outlast the runner's worst-case jittered reconnect delay.

    Runners back off to ``_MAX_RECONNECT_DELAY_S`` with up to
    ``_RECONNECT_JITTER_FRACTION`` added jitter. If the grace is shorter than
    that ceiling the server marks the session failed before a runner at full
    backoff can reconnect. Pins the invariant so an inadvertent reduction of
    the constant is caught immediately.
    """
    from omnigent.runner.transports.ws_tunnel.serve import (
        _MAX_RECONNECT_DELAY_S,
        _RECONNECT_JITTER_FRACTION,
    )
    from omnigent.server.routes._sessions.orchestration import RUNNER_DISCONNECT_GRACE_S

    worst_case_reconnect_s = _MAX_RECONNECT_DELAY_S * (1 + _RECONNECT_JITTER_FRACTION)
    assert worst_case_reconnect_s < RUNNER_DISCONNECT_GRACE_S, (
        f"RUNNER_DISCONNECT_GRACE_S ({RUNNER_DISCONNECT_GRACE_S}s) must exceed "
        f"the runner worst-case reconnect delay "
        f"({_MAX_RECONNECT_DELAY_S} * (1 + {_RECONNECT_JITTER_FRACTION}) = "
        f"{worst_case_reconnect_s}s)"
    )
