"""
End-to-end OTLP round-trip test for the OTel observability series.

Drives the production span emission path (omnigent.inner.tracing.TracingContext)
+ the production metric emission helpers (omnigent.runtime.telemetry) through
a real OTel SDK pipeline pointed at an in-process OTLP/HTTP receiver. Asserts
on the actual protobuf payloads the receiver decodes off the wire.

Verifies the contract of the four-PR OTel series end-to-end:
- PR #1050: AGENT and TOOL spans carry the gen_ai.* semconv attributes
- PR #1072: gen_ai.client.token.usage, gen_ai.client.operation.duration,
  and omnigent.tool.duration metric histograms emit with the right
  dimensions
- PR #1070 (scope C): subprocess-env helper forwards OTel exporter knobs
  to executor subprocesses but does not inject TRACEPARENT

Runs only when collected with -m e2e (omnigent's default pytest config
ignores tests/e2e/. CI opts in via the e2e workflow).
"""

from __future__ import annotations

import time
from collections.abc import Iterator

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace as otel_trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from omnigent.inner.tracing import TracingContext, enable_tracing
from omnigent.runtime import telemetry
from tests.e2e._otlp_receiver import (
    OTLPReceiver,
    attrs_to_dict,
    start_receiver,
)

pytestmark = pytest.mark.e2e


@pytest.fixture
def receiver() -> Iterator[OTLPReceiver]:
    server, _thread = start_receiver()
    try:
        yield server
    finally:
        server.shutdown()


@pytest.fixture
def real_otel_pipeline(
    receiver: OTLPReceiver,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[OTLPReceiver]:
    """
    Install a real TracerProvider + MeterProvider that export to the
    in-process OTLP receiver. Snapshot + restore the previous globals
    on teardown so test isolation holds.
    """
    monkeypatch.setenv("OMNIGENT_OTEL_CAPTURE_CONTENT", "true")
    monkeypatch.setattr("omnigent.runtime.telemetry._capture_content", True)

    span_exporter = OTLPSpanExporter(endpoint=f"{receiver.endpoint}/v1/traces")
    span_processor = BatchSpanProcessor(span_exporter, schedule_delay_millis=50)
    tracer_provider = TracerProvider()
    tracer_provider.add_span_processor(span_processor)

    metric_exporter = OTLPMetricExporter(endpoint=f"{receiver.endpoint}/v1/metrics")
    metric_reader = PeriodicExportingMetricReader(metric_exporter, export_interval_millis=100)
    meter_provider = MeterProvider(metric_readers=[metric_reader])

    previous_tracer = otel_trace._TRACER_PROVIDER  # type: ignore[attr-defined]
    previous_tracer_done = otel_trace._TRACER_PROVIDER_SET_ONCE._done  # type: ignore[attr-defined]
    previous_meter_done = otel_metrics._internal._METER_PROVIDER_SET_ONCE._done  # type: ignore[attr-defined]
    previous_meter = otel_metrics.get_meter_provider()

    otel_trace._TRACER_PROVIDER = tracer_provider  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = True  # type: ignore[attr-defined]
    otel_metrics._internal._METER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    otel_metrics.set_meter_provider(meter_provider)

    telemetry._reset_instrument_cache_for_tests()
    enable_tracing()

    try:
        yield receiver
    finally:
        try:
            tracer_provider.shutdown()
            meter_provider.shutdown()
        except Exception:
            pass
        otel_trace._TRACER_PROVIDER = previous_tracer  # type: ignore[attr-defined]
        otel_trace._TRACER_PROVIDER_SET_ONCE._done = previous_tracer_done  # type: ignore[attr-defined]
        otel_metrics._internal._METER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
        otel_metrics.set_meter_provider(previous_meter)
        otel_metrics._internal._METER_PROVIDER_SET_ONCE._done = previous_meter_done  # type: ignore[attr-defined]
        telemetry._reset_instrument_cache_for_tests()


def _wait_for_spans(receiver: OTLPReceiver, expected: int, timeout_s: float = 5.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if len(receiver.all_spans()) >= expected:
            return
        time.sleep(0.05)
    raise AssertionError(
        f"timed out waiting for {expected} spans. Saw {len(receiver.all_spans())}"
    )


def _wait_for_metric(receiver: OTLPReceiver, metric_name: str, timeout_s: float = 5.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if receiver.all_metric_data_points(metric_name):
            return
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for metric {metric_name}")


# -----------------------------------------------------------------
# Spans (PR #1050)
# -----------------------------------------------------------------


def test_agent_and_tool_spans_export_with_gen_ai_attrs_over_real_otlp(
    real_otel_pipeline: OTLPReceiver,
):
    """
    Real OTel SDK -> BatchSpanProcessor -> OTLPSpanExporter -> in-process
    OTLP/HTTP receiver -> protobuf parse. The decoded payload must carry
    the gen_ai.* semconv attributes that PR #1050 sets on AGENT and TOOL
    spans, plus the OpenInference span-kind attrs main already set.
    """
    ctx = TracingContext()
    agent = ctx.start_agent_span(
        agent_name="debby",
        user_message="What is 2 + 2?",
        model="anthropic/claude-3-5-haiku-20241022",
    )
    tool = ctx.start_tool_span(tool_name="calculator", tool_args={"x": 2, "y": 2})
    ctx.end_tool_span(tool, result={"answer": 4}, duration_ms=12.3)
    ctx.end_agent_span(agent, response="The answer is 4.")

    _wait_for_spans(real_otel_pipeline, expected=2)

    spans = real_otel_pipeline.all_spans()
    by_name = {s.name: s for s in spans}
    assert "agent:debby" in by_name, f"expected agent span, saw {sorted(by_name)}"
    assert "tool:calculator" in by_name, f"expected tool span, saw {sorted(by_name)}"

    agent_attrs = attrs_to_dict(by_name["agent:debby"].attributes)
    assert agent_attrs["gen_ai.operation.name"] == "invoke_agent"
    assert agent_attrs["gen_ai.agent.name"] == "debby"
    assert agent_attrs["gen_ai.provider.name"] == "anthropic"
    assert agent_attrs["gen_ai.request.model"] == "claude-3-5-haiku-20241022"
    assert agent_attrs["openinference.span.kind"] == "AGENT"

    tool_attrs = attrs_to_dict(by_name["tool:calculator"].attributes)
    assert tool_attrs["gen_ai.operation.name"] == "execute_tool"
    assert tool_attrs["tool.name"] == "calculator"


# -----------------------------------------------------------------
# Metrics (PR #1072)
# -----------------------------------------------------------------


def test_token_usage_metric_exports_over_real_otlp(
    real_otel_pipeline: OTLPReceiver,
):
    """
    record_llm_usage -> Histogram.record -> PeriodicExportingMetricReader
    -> OTLPMetricExporter -> in-process receiver -> protobuf parse.
    Verifies two data points (input + output) with provider/model dims.
    """
    tracer = otel_trace.get_tracer("test")
    with tracer.start_as_current_span("agent:test") as span:
        span.set_attribute("gen_ai.provider.name", "anthropic")
        span.set_attribute("gen_ai.request.model", "claude-3-5-haiku-20241022")
        telemetry.record_llm_usage(
            span,
            {"input_tokens": 250, "output_tokens": 80, "total_tokens": 330},
        )

    _wait_for_metric(real_otel_pipeline, "gen_ai.client.token.usage")

    points = real_otel_pipeline.all_metric_data_points("gen_ai.client.token.usage")
    by_type = {attrs_to_dict(p.attributes)["gen_ai.token.type"]: p for p in points}
    assert by_type["input"].sum == 250
    assert by_type["output"].sum == 80
    input_attrs = attrs_to_dict(by_type["input"].attributes)
    assert input_attrs["gen_ai.provider.name"] == "anthropic"
    assert input_attrs["gen_ai.request.model"] == "claude-3-5-haiku-20241022"


def test_operation_and_tool_duration_metrics_export_over_real_otlp(
    real_otel_pipeline: OTLPReceiver,
):
    """
    end_agent_span and end_tool_span (production code paths called from
    _executor_adapter.py) emit operation.duration + tool.duration metric
    data points. Verify they land on the wire with the expected attrs.
    """
    ctx = TracingContext()
    agent = ctx.start_agent_span(agent_name="m", user_message="hi", model="openai/gpt-5.1")
    tool = ctx.start_tool_span(tool_name="calculator", tool_args={"x": 1})
    ctx.end_tool_span(tool, result={"r": 1}, duration_ms=15.0)
    ctx.end_agent_span(agent, response="done")

    _wait_for_metric(real_otel_pipeline, "gen_ai.client.operation.duration")
    _wait_for_metric(real_otel_pipeline, "omnigent.tool.duration")

    op_points = real_otel_pipeline.all_metric_data_points("gen_ai.client.operation.duration")
    assert len(op_points) >= 1
    op_attrs = attrs_to_dict(op_points[0].attributes)
    assert op_attrs["gen_ai.operation.name"] == "invoke_agent"
    assert op_attrs["gen_ai.provider.name"] == "openai"
    assert op_attrs["gen_ai.request.model"] == "gpt-5.1"
    assert op_points[0].sum > 0  # real wall-clock duration

    tool_points = real_otel_pipeline.all_metric_data_points("omnigent.tool.duration")
    assert len(tool_points) >= 1
    tool_attrs = attrs_to_dict(tool_points[0].attributes)
    assert tool_attrs["tool.name"] == "calculator"
    assert tool_points[0].sum == pytest.approx(0.015, abs=1e-3)


# -----------------------------------------------------------------
# Subprocess-env helper (PR #1070 C scope)
# -----------------------------------------------------------------


def test_subprocess_env_forwards_otlp_knobs_without_traceparent(
    real_otel_pipeline: OTLPReceiver,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    Per PR #1070 scope C: get_otel_subprocess_env forwards the OTel
    exporter knobs but never injects TRACEPARENT (subprocess is
    long-running per session. Per-request trace context belongs in
    the SDK channel work tracked separately).
    """
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", real_otel_pipeline.endpoint)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")

    # Drive the helper while a span is active. Even with an active
    # span, TRACEPARENT must be absent from the returned env.
    tracer = otel_trace.get_tracer("test")
    with tracer.start_as_current_span("agent:active"):
        env = telemetry.get_otel_subprocess_env(claude_sdk=True)

    assert "TRACEPARENT" not in env
    assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == real_otel_pipeline.endpoint
    assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/protobuf"
    assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
    assert env["OTEL_TRACES_EXPORTER"] == "otlp"
