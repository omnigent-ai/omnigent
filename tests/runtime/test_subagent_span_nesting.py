"""
Tests for sub-agent span nesting across the dispatch boundary.

Covers the contract that a child session's agent span nests under the
parent's dispatching ``tool:sys_session_send`` span (same trace) and
survives the edge cases: parent completing before the child, tracing
off, content capture off, and re-sends. Exercises the production
pieces individually — the telemetry traceparent helpers, the executor
adapter's dispatch-side capture, the scaffold's action_required item
stamping, and the runner's per-child stash — plus the full nested
export shape via an in-memory OTel exporter.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Iterator

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from omnigent.inner.tracing import (
    TracingContext,
    disable_tracing,
    enable_tracing,
    is_tracing_enabled,
)
from omnigent.runtime.telemetry import (
    trace_context_from_traceparent,
    traceparent_from_span,
)


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    """
    Install a fresh TracerProvider with an in-memory exporter for one
    test, restoring the previous provider on teardown (OTel's set-once
    semantics would otherwise leak into later tests).
    """
    previous = otel_trace._TRACER_PROVIDER  # type: ignore[attr-defined]
    previous_done = otel_trace._TRACER_PROVIDER_SET_ONCE._done  # type: ignore[attr-defined]
    tracing_was_enabled = is_tracing_enabled()
    in_mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(in_mem))
    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = True  # type: ignore[attr-defined]
    enable_tracing()
    try:
        yield in_mem
    finally:
        in_mem.clear()
        with contextlib.suppress(Exception):
            provider.shutdown()
        otel_trace._TRACER_PROVIDER = previous  # type: ignore[attr-defined]
        otel_trace._TRACER_PROVIDER_SET_ONCE._done = previous_done  # type: ignore[attr-defined]
        # Restore the global tracing flag — leaving it enabled would make
        # unrelated tests in the same process build real trace contexts
        # (and choke on non-hex response ids).
        if tracing_was_enabled:
            enable_tracing()
        else:
            disable_tracing()


def _expected_traceparent(span_ctx) -> str:  # type: ignore[no-untyped-def]
    """W3C traceparent for a SpanContext; flags come from the SDK (the
    provider may set the random-trace-id bit, yielding 03 not 01)."""
    return f"00-{span_ctx.trace_id:032x}-{span_ctx.span_id:016x}-{int(span_ctx.trace_flags):02x}"


# ---
# traceparent helpers
# ---


def test_traceparent_from_span_round_trips_ids(exporter: InMemorySpanExporter) -> None:
    ctx = TracingContext()
    span = ctx.start_agent_span(agent_name="a", user_message="hi")
    tp = traceparent_from_span(span)
    ctx.end_agent_span(span, response="done")

    span_ctx = span.get_span_context()
    assert tp == _expected_traceparent(span_ctx)


def test_traceparent_from_span_invalid_context_returns_none() -> None:
    from opentelemetry.trace import INVALID_SPAN

    assert traceparent_from_span(INVALID_SPAN) is None


def test_trace_context_from_traceparent_nests_new_spans(
    exporter: InMemorySpanExporter,
) -> None:
    remote_trace_id = "0af7651916cd43dd8448eb211c80319c"
    remote_span_id = "b7ad6b7169203331"
    with trace_context_from_traceparent(f"00-{remote_trace_id}-{remote_span_id}-01"):
        ctx = TracingContext()
        span = ctx.start_agent_span(agent_name="child", user_message="go")
        ctx.end_agent_span(span, response="ok")

    (exported,) = exporter.get_finished_spans()
    assert format(exported.context.trace_id, "032x") == remote_trace_id
    assert exported.parent is not None
    assert format(exported.parent.span_id, "016x") == remote_span_id


def test_trace_context_from_traceparent_garbage_roots_fresh_trace(
    exporter: InMemorySpanExporter,
) -> None:
    with trace_context_from_traceparent("not-a-traceparent"):
        ctx = TracingContext()
        span = ctx.start_agent_span(agent_name="child", user_message="go")
        ctx.end_agent_span(span, response="ok")

    (exported,) = exporter.get_finished_spans()
    assert exported.context.trace_id != 0
    assert exported.parent is None


# ---
# parent completes before the child
# ---


def test_parent_completion_before_child_still_exports_nested(
    exporter: InMemorySpanExporter,
) -> None:
    """
    The tool span ends at launch and the parent agent span can end
    before the child runs; the child agent span still exports with the
    ended tool span as its remote parent (OTel allows children of
    ended parents via a remote SpanContext).
    """
    parent_ctx = TracingContext(session_id="conv_parent")
    parent_agent = parent_ctx.start_agent_span(agent_name="parent", user_message="dispatch")
    parent_tool = parent_ctx.start_tool_span("sys_session_send", {"agent": "worker"})
    tp = traceparent_from_span(parent_tool)
    assert tp is not None
    parent_ctx.end_tool_span(parent_tool, result="launching", parent_span=parent_agent)
    parent_ctx.end_agent_span(parent_agent, response="dispatched")

    # Parent's spans are fully exported before the child starts.
    assert {s.name for s in exporter.get_finished_spans()} == {
        "agent:parent",
        "tool:sys_session_send",
    }

    with trace_context_from_traceparent(tp):
        child_ctx = TracingContext(session_id="conv_child")
        child_agent = child_ctx.start_agent_span(agent_name="worker", user_message="go")
        child_tool = child_ctx.start_tool_span("child_tool", {"x": 1})
        child_ctx.end_tool_span(child_tool, result="ok", parent_span=child_agent)
        child_ctx.end_agent_span(child_agent, response="did work")

    spans = {s.name: s for s in exporter.get_finished_spans()}
    tool_span = spans["tool:sys_session_send"]
    child_agent_span = spans["agent:worker"]
    child_tool_span = spans["tool:child_tool"]
    assert child_agent_span.context.trace_id == tool_span.context.trace_id
    assert child_agent_span.parent is not None
    assert child_agent_span.parent.span_id == tool_span.context.span_id
    assert child_tool_span.parent is not None
    assert child_tool_span.parent.span_id == child_agent_span.context.span_id
    # The tool span closed at launch — before the child agent span —
    # and there is exactly one child agent span (no second wrapper).
    assert tool_span.end_time < child_agent_span.end_time
    assert len([s for s in spans.values() if s.name == "agent:worker"]) == 1


def test_resend_nests_each_child_turn_under_its_own_tool_span(
    exporter: InMemorySpanExporter,
) -> None:
    parent_ctx = TracingContext(session_id="conv_parent")
    parent_agent = parent_ctx.start_agent_span(agent_name="parent", user_message="dispatch")

    traceparents: list[str] = []
    for _ in range(2):
        tool = parent_ctx.start_tool_span("sys_session_send", {"agent": "worker"})
        tp = traceparent_from_span(tool)
        assert tp is not None
        traceparents.append(tp)
        parent_ctx.end_tool_span(tool, result="launching", parent_span=parent_agent)
    parent_ctx.end_agent_span(parent_agent, response="done")

    for turn, tp in enumerate(traceparents):
        with trace_context_from_traceparent(tp):
            child_ctx = TracingContext(session_id="conv_child")
            span = child_ctx.start_agent_span(agent_name="worker", user_message=f"turn {turn}")
            child_ctx.end_agent_span(span, response=f"result {turn}")

    tool_span_ids = [
        s.context.span_id
        for s in exporter.get_finished_spans()
        if s.name == "tool:sys_session_send"
    ]
    child_parent_ids = [
        s.parent.span_id
        for s in exporter.get_finished_spans()
        if s.name == "agent:worker" and s.parent is not None
    ]
    assert sorted(child_parent_ids) == sorted(tool_span_ids)


# ---
# content capture off
# ---


def test_content_capture_off_child_still_nests_without_payloads(
    exporter: InMemorySpanExporter,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.runtime.telemetry._capture_content", False)

    parent_ctx = TracingContext()
    parent_tool = parent_ctx.start_tool_span("sys_session_send", {"agent": "worker"})
    tp = traceparent_from_span(parent_tool)
    assert tp is not None
    parent_ctx.end_tool_span(parent_tool, result="launching")

    with trace_context_from_traceparent(tp):
        child_ctx = TracingContext()
        span = child_ctx.start_agent_span(agent_name="worker", user_message="secret")
        child_ctx.end_agent_span(span, response="secret result")

    spans = {s.name: s for s in exporter.get_finished_spans()}
    child_agent_span = spans["agent:worker"]
    tool_span = spans["tool:sys_session_send"]
    assert child_agent_span.parent is not None
    assert child_agent_span.parent.span_id == tool_span.context.span_id
    attrs = dict(child_agent_span.attributes or {})
    assert "input.value" not in attrs
    assert "output.value" not in attrs


# ---
# adapter dispatch-side traceparent capture
# ---


def _adapter_with_tracing(tctx: TracingContext):  # type: ignore[no-untyped-def]
    from omnigent.inner.executor import MockExecutor
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=MockExecutor)
    adapter._tracing_ctx = tctx
    return adapter


def test_dispatch_traceparent_prefers_matching_tool_span(
    exporter: InMemorySpanExporter,
) -> None:
    tctx = TracingContext()
    agent = tctx.start_agent_span(agent_name="parent", user_message="hi")
    tool = tctx.start_tool_span("sys_session_send", {"agent": "worker"})
    adapter = _adapter_with_tracing(tctx)

    tp = adapter._dispatch_traceparent("sys_session_send")

    tool_ctx = tool.get_span_context()
    assert tp == _expected_traceparent(tool_ctx)
    tctx.end_tool_span(tool, result="ok", parent_span=agent)
    tctx.end_agent_span(agent, response="done")


def test_dispatch_traceparent_falls_back_to_agent_span(
    exporter: InMemorySpanExporter,
) -> None:
    """A mismatched current span (dispatch raced ahead, or a parallel
    tool moved the cursor) falls back to the turn's agent span."""
    tctx = TracingContext()
    agent = tctx.start_agent_span(agent_name="parent", user_message="hi")
    other_tool = tctx.start_tool_span("sys_os_shell", {"cmd": "ls"})
    adapter = _adapter_with_tracing(tctx)

    tp = adapter._dispatch_traceparent("sys_session_send")

    agent_ctx = agent.get_span_context()
    assert tp == _expected_traceparent(agent_ctx)
    tctx.end_tool_span(other_tool, result="ok", parent_span=agent)
    tctx.end_agent_span(agent, response="done")


def test_dispatch_traceparent_none_for_non_spawning_tool(
    exporter: InMemorySpanExporter,
) -> None:
    tctx = TracingContext()
    agent = tctx.start_agent_span(agent_name="parent", user_message="hi")
    adapter = _adapter_with_tracing(tctx)

    assert adapter._dispatch_traceparent("sys_os_shell") is None
    tctx.end_agent_span(agent, response="done")


def test_dispatch_traceparent_none_when_tracing_inactive() -> None:
    from omnigent.inner.executor import MockExecutor
    from omnigent.runtime.harnesses._executor_adapter import ExecutorAdapter

    adapter = ExecutorAdapter(executor_factory=MockExecutor)
    assert adapter._dispatch_traceparent("sys_session_send") is None


# ---
# scaffold action_required item stamping
# ---


def _make_turn_context():  # type: ignore[no-untyped-def]
    from omnigent.runtime.harnesses._scaffold import TurnContext

    queue: asyncio.Queue = asyncio.Queue()
    return TurnContext(
        response_id="resp_test",
        event_queue=queue,
        cancelled=asyncio.Event(),
    )


@pytest.mark.asyncio
async def test_dispatch_tool_stamps_traceparent_on_action_required_item() -> None:
    ctx = _make_turn_context()
    task = asyncio.create_task(
        ctx.dispatch_tool(
            call_id="call_1",
            name="sys_session_send",
            arguments="{}",
            agent="parent",
            traceparent="00-" + "a" * 32 + "-" + "b" * 16 + "-01",
        )
    )
    event = await asyncio.wait_for(ctx._event_queue.get(), timeout=2)
    assert event.item["traceparent"] == "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    ctx._pending_tool_calls["call_1"].set_result("ok")
    assert await asyncio.wait_for(task, timeout=2) == "ok"


@pytest.mark.asyncio
async def test_dispatch_tool_omits_traceparent_when_absent() -> None:
    ctx = _make_turn_context()
    task = asyncio.create_task(
        ctx.dispatch_tool(call_id="call_2", name="sys_os_shell", arguments="{}", agent="parent")
    )
    event = await asyncio.wait_for(ctx._event_queue.get(), timeout=2)
    assert "traceparent" not in event.item
    ctx._pending_tool_calls["call_2"].set_result("ok")
    await asyncio.wait_for(task, timeout=2)


# ---
# runner-side extraction + stash
# ---


def test_get_item_traceparent_extracts_string() -> None:
    from omnigent.runner.tool_dispatch import get_item_traceparent

    tp = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"
    assert get_item_traceparent({"item": {"traceparent": tp}}) == tp
    assert get_item_traceparent({"item": {}}) is None
    assert get_item_traceparent({"item": {"traceparent": ""}}) is None
    assert get_item_traceparent({"item": {"traceparent": 42}}) is None
    assert get_item_traceparent({}) is None


def test_stash_and_pop_child_turn_traceparent() -> None:
    from omnigent.runner import app as runner_app

    tp = "00-" + "c" * 32 + "-" + "d" * 16 + "-01"
    runner_app.stash_child_turn_traceparent("conv_child_x", tp)
    assert runner_app.pop_child_turn_traceparent("conv_child_x") == tp
    # Consumed — a second (non-dispatched) turn gets nothing.
    assert runner_app.pop_child_turn_traceparent("conv_child_x") is None


def test_restash_overwrites_previous_traceparent() -> None:
    from omnigent.runner import app as runner_app

    first = "00-" + "1" * 32 + "-" + "1" * 16 + "-01"
    runner_app.stash_child_turn_traceparent("conv_child_y", first)
    replacement = "00-" + "2" * 32 + "-" + "2" * 16 + "-01"
    runner_app.stash_child_turn_traceparent("conv_child_y", replacement)
    assert runner_app.pop_child_turn_traceparent("conv_child_y") == replacement


def test_unregister_child_session_clears_pending_traceparent() -> None:
    from omnigent.runner import app as runner_app

    traceparent = "00-" + "3" * 32 + "-" + "3" * 16 + "-01"
    runner_app.stash_child_turn_traceparent("conv_child_z", traceparent)
    runner_app.unregister_child_session("conv_child_z")
    assert runner_app.pop_child_turn_traceparent("conv_child_z") is None
