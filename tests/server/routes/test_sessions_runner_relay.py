"""Tests for AP's runner stream relay startup handshake."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace, TracebackType
from typing import Any

import pytest

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


@pytest.mark.asyncio
async def test_relay_publishes_failed_status_on_tunnel_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A tunnel close mid-stream publishes ``session.status`` "failed".

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
        session_stream.close(session_id)


class _RecordingLabelStore:
    """Minimal conversation store that records ``set_labels`` calls.

    The disconnect path persists the failure cause as durable labels so
    snapshots and child summaries can tell a benign runner disconnect
    from a real task failure (Option B). ``set_labels`` is exercised by
    the tunnel-close path; ``get_conversation`` is read by
    ``_publish_runner_recovered_status`` to gate the clear on the
    persisted disconnect code, so both are implemented here.
    """

    def __init__(self) -> None:
        self.labels: dict[str, dict[str, str]] = {}

    def set_labels(self, conversation_id: str, updates: dict[str, str]) -> None:
        self.labels.setdefault(conversation_id, {}).update(updates)

    def get_conversation(self, conversation_id: str) -> Any:
        """Return a conversation-shaped object exposing ``.labels``.

        Only ``.labels`` is read by the recovery guard, so a lightweight
        namespace over the recorded labels is enough.
        """
        return SimpleNamespace(labels=dict(self.labels.get(conversation_id, {})))


@pytest.mark.asyncio
async def test_relay_persists_disconnect_error_labels_on_tunnel_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A tunnel close persists the ``runner_disconnected`` cause as labels.

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
        await asyncio.wait_for(handle.task, timeout=2.0)

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
                await asyncio.wait_for(handle.task, timeout=1.0)
        sessions_module._runner_relay_tasks.clear()
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
    :param gate: Event the test sets once subscribed, gating the drop so
        the collector observes every scripted frame first.
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
        yield 'data: {"type": "session.heartbeat"}\n\n'
        for frame in self._frames:
            yield frame
        await self._gate.wait()
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
        handle = await sessions_module._ensure_runner_relay_ready(
            session_id,
            "runner_flaky_then_healthy",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=store,  # type: ignore[arg-type]
        )
        assert handle is not None
        await asyncio.wait_for(handle.task, timeout=2.0)

        assert fake_runner.calls == 2, "relay did not retry after the drop"
        # The blip resolved silently: no failed status reached the cache
        # and no runner_disconnected labels were persisted.
        assert sessions_module._session_status_cache.get(session_id) is None
        assert store.labels.get(session_id) is None
    finally:
        handle = sessions_module._runner_relay_tasks.get(session_id)
        if handle is not None and not handle.task.done():
            handle.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(handle.task, timeout=1.0)
        sessions_module._runner_relay_tasks.clear()
        sessions_module._session_status_cache.pop(session_id, None)


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
