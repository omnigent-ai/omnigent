"""Skill-execution telemetry in the executor adapter."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from omnigent.inner.executor import (
    ExecutorError,
    ExecutorEvent,
    MockExecutor,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
    TurnCancelled,
    TurnComplete,
)
from omnigent.inner.tracing import disable_tracing, enable_tracing
from omnigent.runtime import skill_metrics, telemetry
from omnigent.runtime.harnesses._executor_adapter import (
    _SKILL_TOOL_NAMES,
    ExecutorAdapter,
    _extract_skill_name,
    _strip_mcp_tool_prefix,
)
from omnigent.runtime.harnesses._scaffold import TurnContext
from omnigent.server.schemas import CreateResponseRequest

# --- Skill-name extraction ---


def test_skill_tool_names_cover_native_and_builtin() -> None:
    """Both Claude Code's native ``Skill`` and Omnigent's ``load_skill`` count."""
    assert "Skill" in _SKILL_TOOL_NAMES
    assert "load_skill" in _SKILL_TOOL_NAMES
    # load_skill arrives MCP-prefixed under the claude-sdk harness.
    assert _strip_mcp_tool_prefix("mcp__omnigent__load_skill") == "load_skill"


def test_extract_skill_name_load_skill_name_key() -> None:
    assert _extract_skill_name("load_skill", {"name": "code-review"}) == "code-review"


def test_extract_skill_name_native_skill_key() -> None:
    assert _extract_skill_name("Skill", {"skill": "deploy", "args": "--force"}) == "deploy"


def test_extract_skill_name_native_plugin_qualified() -> None:
    assert _extract_skill_name("Skill", {"skill": "my-plugin:deploy"}) == "my-plugin:deploy"


def test_extract_skill_name_native_command_key() -> None:
    # Older Claude Code versions send {"command": "<skill>"}.
    assert _extract_skill_name("Skill", {"command": "cardinal"}) == "cardinal"


def test_extract_skill_name_strips_whitespace() -> None:
    assert _extract_skill_name("load_skill", {"name": "  imaforge  "}) == "imaforge"


def test_extract_skill_name_rejects_unrecognized_keys() -> None:
    """Only the tool's name field counts; other args are content and never exported."""
    assert _extract_skill_name("Skill", {"args": "private task details"}) is None
    assert _extract_skill_name("Skill", {"unexpected": "creditflow"}) is None
    # Keys are per tool: load_skill's name field is not Skill's.
    assert _extract_skill_name("Skill", {"name": "deploy"}) is None
    assert _extract_skill_name("load_skill", {"skill": "deploy"}) is None


def test_extract_skill_name_rejects_values_that_are_not_skill_names() -> None:
    """Free text in a name field is rejected, so it can't become an attribute or label."""
    assert _extract_skill_name("Skill", {"skill": "private task details"}) is None
    assert _extract_skill_name("Skill", {"skill": "a" * 200}) is None
    assert _extract_skill_name("Skill", {"skill": "x:y:z"}) is None
    assert _extract_skill_name("load_skill", {"name": "../etc/passwd"}) is None


def test_extract_skill_name_none_when_no_usable_value() -> None:
    assert _extract_skill_name("Skill", {}) is None
    assert _extract_skill_name("Skill", {"skill": 5}) is None
    assert _extract_skill_name("Skill", {"skill": "   "}) is None
    assert _extract_skill_name("Skill", None) is None  # type: ignore[arg-type]


# --- Adapter wiring ---


@dataclass
class _Recorded:
    """Skill metrics captured from the adapter."""

    invocations: list[tuple[str, str]] = field(default_factory=list)
    durations: list[tuple[str, float]] = field(default_factory=list)
    tool_calls: list[tuple[str, str]] = field(default_factory=list)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> _Recorded:
    """Capture the adapter's skill-metric calls."""
    rec = _Recorded()
    monkeypatch.setattr(
        skill_metrics, "record_skill_invocation", lambda s, o: rec.invocations.append((s, o))
    )
    monkeypatch.setattr(
        skill_metrics,
        "record_skill_execution_duration",
        lambda s, d: rec.durations.append((s, d)),
    )
    monkeypatch.setattr(
        skill_metrics, "record_skill_tool_call", lambda s, t: rec.tool_calls.append((s, t))
    )
    return rec


@pytest.fixture
def exporter(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """Enable tracing on a fresh provider with the active-skill processor."""
    monkeypatch.delenv("OMNIGENT_OTEL_CAPTURE_CONTENT", raising=False)
    monkeypatch.setattr(telemetry, "_capture_content", False)
    previous = otel_trace._TRACER_PROVIDER  # type: ignore[attr-defined]
    previous_done = otel_trace._TRACER_PROVIDER_SET_ONCE._done  # type: ignore[attr-defined]
    in_mem = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(telemetry._make_active_skill_processor())
    provider.add_span_processor(SimpleSpanProcessor(in_mem))
    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = True  # type: ignore[attr-defined]
    enable_tracing()
    try:
        yield in_mem
    finally:
        disable_tracing()
        with contextlib.suppress(Exception):
            provider.shutdown()
        otel_trace._TRACER_PROVIDER = previous  # type: ignore[attr-defined]
        otel_trace._TRACER_PROVIDER_SET_ONCE._done = previous_done  # type: ignore[attr-defined]


def _tool(name: str, args: dict[str, object], call_id: str) -> list[ExecutorEvent]:
    """A tool request plus its successful completion."""
    meta = {"call_id": call_id}
    return [
        ToolCallRequest(name=name, args=args, metadata=meta),
        ToolCallComplete(name=name, status=ToolCallStatus.SUCCESS, result="ok", metadata=meta),
    ]


async def _run(events: list[ExecutorEvent]) -> None:
    """Run one adapter turn over *events*."""
    executor = MockExecutor()
    executor.enqueue_events(events)
    adapter = ExecutorAdapter(executor_factory=lambda: executor)
    ctx = TurnContext(
        response_id=f"resp_{uuid.uuid4().hex}",
        event_queue=asyncio.Queue(),
        cancelled=asyncio.Event(),
    )
    try:
        await adapter.run_turn(CreateResponseRequest(model="test-agent", input="hi"), ctx)
    finally:
        await adapter.on_shutdown()


def _tool_spans(exporter: InMemorySpanExporter) -> list[ReadableSpan]:
    return [s for s in exporter.get_finished_spans() if s.name.startswith("tool:")]


@pytest.mark.asyncio
async def test_each_skill_call_is_counted_and_timed(
    exporter: InMemorySpanExporter, recorded: _Recorded
) -> None:
    """Repeated and different skills in one turn each get a count and a duration."""
    await _run(
        [
            *_tool("Skill", {"skill": "a"}, "c1"),
            *_tool("mcp__omnigent__load_skill", {"name": "b"}, "c2"),
            *_tool("Bash", {"command": "ls"}, "c3"),
            *_tool("Skill", {"skill": "a"}, "c4"),
            *_tool("Read", {"path": "x"}, "c5"),
            TurnComplete(response="done"),
        ]
    )

    assert recorded.invocations == [("a", "success"), ("b", "success"), ("a", "success")]
    assert [name for name, _ in recorded.durations] == ["a", "b", "a"]
    assert all(ms >= 0 for _, ms in recorded.durations)
    # Each call is timed from its own start, so earlier calls run longer.
    assert recorded.durations[0][1] >= recorded.durations[2][1]
    # Later tools are attributed to the most recent skill.
    assert recorded.tool_calls == [("b", "Bash"), ("a", "Read")]

    spans = _tool_spans(exporter)
    names = [(s.attributes or {}).get("omnigent.skill.name") for s in spans]
    active = [(s.attributes or {}).get("omnigent.skill.active") for s in spans]
    assert names == ["a", "b", None, "a", None]
    # A skill's own span starts before it activates, so it carries the prior skill.
    assert active == [None, "a", "b", "b", "a"]
    assert telemetry._active_skill_var.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("call_id", "initial_skill", "next_skill", "observations", "expected"),
    [
        pytest.param("shell-1", "a", None, [False, True], ["a"], id="live-and-completed"),
        pytest.param("shell-1", "a", None, [True], ["a"], id="completed-only"),
        pytest.param(None, "a", None, [False, False], ["a", "a"], id="missing-ids"),
        pytest.param("", "a", None, [False, False], ["a", "a"], id="empty-ids"),
        pytest.param("shell-1", None, "a", [False, True], [], id="started-before-skill"),
        pytest.param("shell-1", "a", "b", [False, True], ["a"], id="skill-changed"),
    ],
)
async def test_tool_metrics_count_each_observed_call_once(
    exporter: InMemorySpanExporter,
    recorded: _Recorded,
    call_id: str | None,
    initial_skill: str | None,
    next_skill: str | None,
    observations: list[bool],
    expected: list[str],
) -> None:
    """Codex completion observations preserve the first observation's attribution."""
    del exporter
    events: list[ExecutorEvent] = []
    if initial_skill:
        events.extend(_tool("load_skill", {"name": initial_skill}, "skill-1"))
    for index, completed in enumerate(observations):
        if index and next_skill:
            events.extend(_tool("load_skill", {"name": next_skill}, "skill-2"))
        events.append(
            ToolCallRequest(
                name="shell",
                args={"command": "pwd"},
                metadata={
                    "call_id": call_id,
                    "internally_executed": True,
                    "observed_call_completed": completed,
                },
            )
        )
    events.extend(
        [
            ToolCallComplete(name="shell", result="/tmp", metadata={"call_id": call_id}),
            TurnComplete(response="done"),
        ]
    )
    await _run(events)

    assert recorded.tool_calls == [(skill, "shell") for skill in expected]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal", "outcome"),
    [
        (TurnComplete(response="done"), "success"),
        (TurnCancelled(reason="provider_cancelled"), "cancelled"),
        (ExecutorError(message="boom"), "error"),
    ],
)
async def test_turn_outcome_and_active_skill_cleanup(
    exporter: InMemorySpanExporter,
    recorded: _Recorded,
    terminal: ExecutorEvent,
    outcome: str,
) -> None:
    """The turn's outcome labels the invocation and the active skill is always released."""
    del exporter
    with (
        pytest.raises(RuntimeError, match="inner executor error: boom")
        if outcome == "error"
        else contextlib.nullcontext()
    ):
        await _run([*_tool("Skill", {"skill": "deploy"}, "c1"), terminal])

    assert recorded.invocations == [("deploy", outcome)]
    assert telemetry._active_skill_var.get() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args",
    [{"args": "private task details"}, {"skill": "private task details"}],
)
async def test_malformed_skill_call_exports_no_argument_content(
    exporter: InMemorySpanExporter, recorded: _Recorded, args: dict[str, object]
) -> None:
    """With content capture off, a malformed skill call leaks nothing into telemetry."""
    await _run([*_tool("Skill", args, "c1"), *_tool("Bash", {}, "c2"), TurnComplete(response="")])

    assert recorded == _Recorded()
    for span in exporter.get_finished_spans():
        for value in (span.attributes or {}).values():
            assert "private task details" not in str(value)
    assert telemetry._active_skill_var.get() is None
