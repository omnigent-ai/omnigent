"""
Tests for the GenAI semantic-convention attributes and the
content-capture gating added to ``omnigent.inner.tracing``.

Span-emission verification uses an in-memory OTel exporter so the
tests have no external dependencies.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import trace as otel_trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from omnigent.inner.tracing import TracingContext
from omnigent.runtime import telemetry


@pytest.fixture
def in_memory_exporter(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[InMemorySpanExporter]:
    """
    Install a fresh in-memory OTel exporter and reset MLflow tracing
    state. Mirrors the pattern in ``tests/runtime/test_telemetry.py`` so
    span assertions work without touching a real backend.
    """
    monkeypatch.setenv("MLFLOW_USE_DEFAULT_TRACER_PROVIDER", "false")
    telemetry._initialized = False  # type: ignore[attr-defined]

    from mlflow.tracing.trace_manager import InMemoryTraceManager

    inst = getattr(InMemoryTraceManager, "_instance", None)
    if inst is not None:
        inst._traces.clear()  # type: ignore[attr-defined]
        inst._otel_id_to_mlflow_trace_id.clear()  # type: ignore[attr-defined]

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    otel_trace._TRACER_PROVIDER = provider  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = True  # type: ignore[attr-defined]

    from mlflow.tracing.provider import provider as mp

    mp._global_provider_init_once._done = False  # type: ignore[attr-defined]

    import mlflow.tracing

    mlflow.tracing.enable()
    yield exporter
    exporter.clear()


def _decoded_attrs(span: Any) -> dict[str, Any]:
    """
    Return span attributes with JSON-encoded string values decoded.

    MLflow's OTel span processor JSON-encodes string attribute values
    before OTLP export, so ``"foo"`` arrives as the literal ``'"foo"'``.
    Tests assert on logical values, so each string attribute is
    round-tripped through ``json.loads`` when possible.
    """
    raw = dict(span.attributes or {})
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if isinstance(v, str):
            try:
                out[k] = json.loads(v)
            except (json.JSONDecodeError, ValueError):
                out[k] = v
        else:
            out[k] = v
    return out


def _raw_attr_text(span: Any, key: str) -> str:
    """
    Return a span attribute as a raw string for substring checks.

    The content-capture tests check whether sensitive substrings appear
    anywhere in the serialized inputs / outputs payload, regardless of
    JSON structure. This helper returns the verbatim attribute or an
    empty string when unset.
    """
    raw = dict(span.attributes or {})
    val = raw.get(key, "")
    return val if isinstance(val, str) else json.dumps(val)


def test_agent_span_sets_genai_attributes(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """Agent spans carry GenAI Agent Spans semconv attributes."""
    monkeypatch.delenv("OMNIGENT_OTEL_CAPTURE_CONTENT", raising=False)
    telemetry.init()

    ctx = TracingContext()
    span_handle = ctx.start_agent_span(
        agent_name="debby",
        user_message="hi",
        model="anthropic/claude-sonnet-4-5",
    )
    ctx.end_agent_span(span_handle, response="hello")

    finished = list(in_memory_exporter.get_finished_spans())
    agent_spans = [s for s in finished if s.name == "agent:debby"]
    assert len(agent_spans) == 1
    attrs = _decoded_attrs(agent_spans[0])
    assert attrs["gen_ai.operation.name"] == "invoke_agent"
    assert attrs["gen_ai.agent.name"] == "debby"
    assert attrs["gen_ai.provider.name"] == "anthropic"
    assert attrs["gen_ai.request.model"] == "claude-sonnet-4-5"
    # Original omnigent attributes remain so MLflow-side rendering is
    # unchanged.
    assert attrs["agent.name"] == "debby"
    assert attrs["model"] == "anthropic/claude-sonnet-4-5"


def test_llm_span_sets_genai_attributes(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """LLM spans carry GenAI chat-span semconv attributes."""
    monkeypatch.delenv("OMNIGENT_OTEL_CAPTURE_CONTENT", raising=False)
    telemetry.init()

    ctx = TracingContext()
    agent = ctx.start_agent_span(agent_name="debby", user_message="hi")
    llm = ctx.start_llm_span(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/gpt-5",
    )
    ctx.end_llm_span(llm, response_text="hello")
    ctx.end_agent_span(agent, response="hello")

    finished = list(in_memory_exporter.get_finished_spans())
    llm_spans = [s for s in finished if s.name == "llm_call"]
    assert len(llm_spans) == 1
    attrs = _decoded_attrs(llm_spans[0])
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.provider.name"] == "openai"
    assert attrs["gen_ai.request.model"] == "gpt-5"


def test_tool_span_sets_genai_attributes(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """Tool spans carry GenAI ``execute_tool`` semconv attributes."""
    monkeypatch.delenv("OMNIGENT_OTEL_CAPTURE_CONTENT", raising=False)
    telemetry.init()

    ctx = TracingContext()
    agent = ctx.start_agent_span(agent_name="debby", user_message="hi")
    tool = ctx.start_tool_span(tool_name="calculator", tool_args={"x": 1})
    ctx.end_tool_span(tool, result={"answer": 1})
    ctx.end_agent_span(agent, response="done")

    finished = list(in_memory_exporter.get_finished_spans())
    tool_spans = [s for s in finished if s.name == "tool:calculator"]
    assert len(tool_spans) == 1
    attrs = _decoded_attrs(tool_spans[0])
    assert attrs["gen_ai.operation.name"] == "execute_tool"
    assert attrs["tool.name"] == "calculator"


def test_unprefixed_model_omits_provider_name(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """A model string with no ``provider/`` prefix omits the provider attr."""
    monkeypatch.delenv("OMNIGENT_OTEL_CAPTURE_CONTENT", raising=False)
    telemetry.init()

    ctx = TracingContext()
    agent = ctx.start_agent_span(agent_name="debby", user_message="hi", model="gpt-5")
    ctx.end_agent_span(agent, response="hi")

    finished = list(in_memory_exporter.get_finished_spans())
    agent_spans = [s for s in finished if s.name == "agent:debby"]
    attrs = _decoded_attrs(agent_spans[0])
    assert attrs["gen_ai.request.model"] == "gpt-5"
    assert "gen_ai.provider.name" not in attrs


def test_content_capture_disabled_omits_sensitive_payloads(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """With ``OMNIGENT_OTEL_CAPTURE_CONTENT`` unset, span content is not captured."""
    monkeypatch.delenv("OMNIGENT_OTEL_CAPTURE_CONTENT", raising=False)
    telemetry.init()
    assert telemetry.should_capture_content() is False

    ctx = TracingContext()
    agent = ctx.start_agent_span(agent_name="debby", user_message="secret PII")
    llm = ctx.start_llm_span(
        messages=[{"role": "user", "content": "secret PII"}], model="openai/gpt-5"
    )
    ctx.end_llm_span(llm, response_text="secret response")
    tool = ctx.start_tool_span(tool_name="calc", tool_args={"key": "sk-secret"})
    ctx.end_tool_span(tool, result={"answer": "secret data"})
    ctx.end_agent_span(agent, response="secret response")

    finished = list(in_memory_exporter.get_finished_spans())
    for s in finished:
        inputs = _raw_attr_text(s, "mlflow.spanInputs")
        outputs = _raw_attr_text(s, "mlflow.spanOutputs")
        assert "secret PII" not in inputs
        assert "secret PII" not in outputs
        assert "secret response" not in inputs
        assert "secret response" not in outputs
        assert "sk-secret" not in inputs
        assert "sk-secret" not in outputs
        assert "secret data" not in inputs
        assert "secret data" not in outputs


def test_content_capture_enabled_includes_payloads(
    monkeypatch: pytest.MonkeyPatch,
    in_memory_exporter: InMemorySpanExporter,
) -> None:
    """With ``OMNIGENT_OTEL_CAPTURE_CONTENT=true``, content flows into spans."""
    monkeypatch.setenv("OMNIGENT_OTEL_CAPTURE_CONTENT", "true")
    telemetry.init()
    assert telemetry.should_capture_content() is True

    ctx = TracingContext()
    agent = ctx.start_agent_span(agent_name="debby", user_message="hello world")
    llm = ctx.start_llm_span(
        messages=[{"role": "user", "content": "hello world"}], model="openai/gpt-5"
    )
    ctx.end_llm_span(llm, response_text="hi there")
    ctx.end_agent_span(agent, response="hi there")

    finished = list(in_memory_exporter.get_finished_spans())
    agent_span = next(s for s in finished if s.name == "agent:debby")
    llm_span = next(s for s in finished if s.name == "llm_call")
    assert "hello world" in _raw_attr_text(agent_span, "mlflow.spanInputs")
    assert "hi there" in _raw_attr_text(llm_span, "mlflow.spanOutputs")
