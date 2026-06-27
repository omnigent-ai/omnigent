"""Tests for AP's runner stream relay startup handshake."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from types import TracebackType
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
            ``"/v1/sessions/conv_abc/stream"``.
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
            "conv_ready",
            "runner_ready",
            fake_runner,  # type: ignore[arg-type]
            conversation_store=None,
        )

        assert handle is not None
        assert handle.ready.is_set()
        assert fake_runner.stream_calls[0][0] == "GET"
        assert fake_runner.stream_calls[0][1] == "/v1/sessions/conv_ready/stream"
    finally:
        release.set()
        handle = sessions_module._runner_relay_tasks.get("conv_ready")
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
            ``"/v1/sessions/conv_abc/stream"``.
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
async def test_relay_publishes_failed_status_on_tunnel_close() -> None:
    """
    A tunnel close mid-stream publishes ``session.status`` "failed".

    Regression test for #1114: before the fix the relay swallowed the
    ``ConnectionError`` and exited silently, leaving the client's SSE
    stream truncated with no error event.
    """
    from omnigent.runtime import session_stream
    from omnigent.server.routes import sessions as sessions_module

    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    fake_runner = _TunnelCloseRunnerClient(gate)
    session_id = "conv_tunnel_close"

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
    from a real task failure (Option B). Only ``set_labels`` is exercised
    by the tunnel-close path, so that is all this fake implements.
    """

    def __init__(self) -> None:
        self.labels: dict[str, dict[str, str]] = {}

    def set_labels(self, conversation_id: str, updates: dict[str, str]) -> None:
        self.labels.setdefault(conversation_id, {}).update(updates)


@pytest.mark.asyncio
async def test_relay_persists_disconnect_error_labels_on_tunnel_close() -> None:
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

    sessions_module._runner_relay_tasks.clear()
    gate = asyncio.Event()
    fake_runner = _TunnelCloseRunnerClient(gate)
    store = _RecordingLabelStore()
    session_id = "conv_tunnel_close_labels"

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
