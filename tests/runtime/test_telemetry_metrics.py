"""
Tests for the GenAI metric instruments emitted by
omnigent.runtime.telemetry and omnigent.inner.tracing (PR #1072).

Each test installs a fresh MeterProvider with an InMemoryMetricReader
through the OTel public API, runs the production code path, then
asserts on the captured data points.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from omnigent.runtime import telemetry


@pytest.fixture
def metric_reader() -> Iterator[InMemoryMetricReader]:
    """
    Install a fresh MeterProvider for one test and yield the reader.

    Snapshots the previous provider + clears omnigent.runtime.telemetry's
    lazy instrument cache so the next record call rebinds against this
    test's MeterProvider rather than the singleton from a previous run.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    previous = otel_metrics.get_meter_provider()
    # _METER_PROVIDER_SET_ONCE behaviour: set_meter_provider only
    # succeeds the first time per process. Force the assignment via
    # the private attribute for test isolation. Process-serial only;
    # not safe under pytest-xdist parallel test workers.
    otel_metrics._internal._METER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    otel_metrics.set_meter_provider(provider)
    telemetry._reset_instrument_cache_for_tests()

    try:
        yield reader
    finally:
        try:
            provider.shutdown()
        except Exception:
            pass
        otel_metrics._internal._METER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
        otel_metrics.set_meter_provider(previous)
        telemetry._reset_instrument_cache_for_tests()


def _datapoints_by_metric(reader: InMemoryMetricReader, name: str):
    """Extract all data points for a given metric name across all resources."""
    data = reader.get_metrics_data()
    if data is None:
        return []
    points = []
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                if metric.name == name:
                    points.extend(metric.data.data_points)
    return points


def test_record_token_usage_metric_emits_two_data_points(
    metric_reader: InMemoryMetricReader,
):
    """One data point per non-None token count, distinguished by gen_ai.token.type."""
    telemetry.record_token_usage_metric(
        input_tokens=100,
        output_tokens=50,
        provider="anthropic",
        model="claude-3-5-haiku-20241022",
    )

    points = _datapoints_by_metric(metric_reader, "gen_ai.client.token.usage")
    assert len(points) == 2
    by_type = {p.attributes["gen_ai.token.type"]: p for p in points}
    assert by_type["input"].sum == 100
    assert by_type["output"].sum == 50
    assert by_type["input"].attributes["gen_ai.provider.name"] == "anthropic"
    assert by_type["input"].attributes["gen_ai.request.model"] == "claude-3-5-haiku-20241022"


def test_record_token_usage_metric_handles_none_counts(
    metric_reader: InMemoryMetricReader,
):
    """None token counts produce zero data points (no invented zeros)."""
    telemetry.record_token_usage_metric(input_tokens=None, output_tokens=None)
    points = _datapoints_by_metric(metric_reader, "gen_ai.client.token.usage")
    assert points == []


def test_record_token_usage_metric_silent_on_bad_input(
    metric_reader: InMemoryMetricReader,
):
    """Non-numeric token counts are silently dropped, not raised."""
    telemetry.record_token_usage_metric(input_tokens="not-a-number", output_tokens=10)  # type: ignore[arg-type]
    points = _datapoints_by_metric(metric_reader, "gen_ai.client.token.usage")
    # Input was dropped via the ValueError path; output never recorded
    # because the input path returned before reaching the output branch.
    # The exact data-point count depends on whether the helper short-
    # circuits after the first invalid input. Either way, no exception
    # crosses into the caller.
    assert all(p.attributes.get("gen_ai.token.type") != "input" for p in points)


def test_record_operation_duration_metric_emits_one_data_point(
    metric_reader: InMemoryMetricReader,
):
    telemetry.record_operation_duration_metric(
        duration_seconds=1.25,
        provider="openai",
        model="gpt-5.1",
        error_type=None,
    )
    points = _datapoints_by_metric(metric_reader, "gen_ai.client.operation.duration")
    assert len(points) == 1
    point = points[0]
    assert point.sum == 1.25
    assert point.attributes["gen_ai.operation.name"] == "invoke_agent"
    assert point.attributes["gen_ai.provider.name"] == "openai"
    assert point.attributes["gen_ai.request.model"] == "gpt-5.1"


def test_record_operation_duration_metric_records_error_type(
    metric_reader: InMemoryMetricReader,
):
    telemetry.record_operation_duration_metric(
        duration_seconds=0.5, error_type="timeout"
    )
    points = _datapoints_by_metric(metric_reader, "gen_ai.client.operation.duration")
    assert points[0].attributes["error.type"] == "timeout"


def test_record_tool_duration_metric_emits_one_data_point(
    metric_reader: InMemoryMetricReader,
):
    telemetry.record_tool_duration_metric(tool_name="calculator", duration_seconds=0.05)
    points = _datapoints_by_metric(metric_reader, "omnigent.tool.duration")
    assert len(points) == 1
    assert points[0].sum == 0.05
    assert points[0].attributes["tool.name"] == "calculator"


def test_tool_duration_metric_buckets_unlisted_names_to_other_with_allowlist(
    metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
):
    """
    With OMNIGENT_TOOL_METRIC_ALLOWLIST set, tool names outside the
    list bucket to '_other'. Bounds cardinality when MCP tools are
    user-defined.
    """
    monkeypatch.setenv("OMNIGENT_TOOL_METRIC_ALLOWLIST", "calculator,search")
    telemetry._reset_instrument_cache_for_tests()  # re-read env

    telemetry.record_tool_duration_metric(tool_name="calculator", duration_seconds=0.01)
    telemetry.record_tool_duration_metric(tool_name="search", duration_seconds=0.02)
    telemetry.record_tool_duration_metric(
        tool_name="some-mcp-tool-from-user-config", duration_seconds=0.03
    )

    points = _datapoints_by_metric(metric_reader, "omnigent.tool.duration")
    names = sorted(p.attributes["tool.name"] for p in points)
    assert names == ["_other", "calculator", "search"]


def test_tool_duration_metric_no_bucketing_without_allowlist(
    metric_reader: InMemoryMetricReader,
    monkeypatch: pytest.MonkeyPatch,
):
    """Without OMNIGENT_TOOL_METRIC_ALLOWLIST, all tool names pass through."""
    monkeypatch.delenv("OMNIGENT_TOOL_METRIC_ALLOWLIST", raising=False)
    telemetry._reset_instrument_cache_for_tests()

    telemetry.record_tool_duration_metric(tool_name="exotic-tool-name", duration_seconds=0.01)
    points = _datapoints_by_metric(metric_reader, "omnigent.tool.duration")
    assert points[0].attributes["tool.name"] == "exotic-tool-name"


def test_record_llm_usage_emits_both_span_attrs_and_metric(
    metric_reader: InMemoryMetricReader,
):
    """
    End-to-end through record_llm_usage on a real span: the span gets
    gen_ai.usage.* attributes AND the metric histogram receives the
    paired data points with provider/model dimensions.
    """
    from opentelemetry import trace as otel_trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    span_exporter = InMemorySpanExporter()
    span_provider = TracerProvider()
    span_provider.add_span_processor(SimpleSpanProcessor(span_exporter))
    previous_span_provider = otel_trace._TRACER_PROVIDER  # type: ignore[attr-defined]
    previous_done = otel_trace._TRACER_PROVIDER_SET_ONCE._done  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER = span_provider  # type: ignore[attr-defined]
    otel_trace._TRACER_PROVIDER_SET_ONCE._done = True  # type: ignore[attr-defined]

    try:
        tracer = otel_trace.get_tracer("test")
        with tracer.start_as_current_span("agent:test") as span:
            span.set_attribute("gen_ai.provider.name", "anthropic")
            span.set_attribute("gen_ai.request.model", "claude-3-5-haiku")
            telemetry.record_llm_usage(
                span,
                {"input_tokens": 200, "output_tokens": 75, "total_tokens": 275},
            )
    finally:
        otel_trace._TRACER_PROVIDER = previous_span_provider  # type: ignore[attr-defined]
        otel_trace._TRACER_PROVIDER_SET_ONCE._done = previous_done  # type: ignore[attr-defined]

    # Span attrs landed
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    attrs = dict(spans[0].attributes or {})
    assert attrs["gen_ai.usage.input_tokens"] == 200
    assert attrs["gen_ai.usage.output_tokens"] == 75
    assert attrs["gen_ai.usage.total_tokens"] == 275

    # Metric data points landed
    points = _datapoints_by_metric(metric_reader, "gen_ai.client.token.usage")
    assert len(points) == 2
    by_type = {p.attributes["gen_ai.token.type"]: p for p in points}
    assert by_type["input"].sum == 200
    assert by_type["output"].sum == 75
    assert by_type["input"].attributes["gen_ai.provider.name"] == "anthropic"
    assert by_type["input"].attributes["gen_ai.request.model"] == "claude-3-5-haiku"
