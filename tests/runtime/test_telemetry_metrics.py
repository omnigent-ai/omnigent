"""
Unit tests for the OTel GenAI metric instruments emitted by
``omnigent.runtime.telemetry``.

Each test installs a fresh ``MeterProvider`` wired to an
``InMemoryMetricReader`` so emissions can be inspected without network
I/O. Tests intentionally exercise the public ``record_*_metric`` helpers
because that's the surface the inner tracing layer calls; the underlying
histogram cache and lazy instrument creation fall out as a side effect.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from omnigent.runtime import telemetry


def _data_points(
    reader: InMemoryMetricReader,
    metric_name: str,
) -> list[Any]:
    """
    Return all histogram data points for a metric name.

    :param reader: In-memory reader the test installed on the provider.
    :param metric_name: Metric name to filter on, e.g.
        ``"gen_ai.client.token.usage"``.
    :returns: Flat list of data points across all resource and scope
        groupings. Empty when the metric was never recorded.
    """
    points: list[Any] = []
    data = reader.get_metrics_data()
    if data is None:
        return points
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == metric_name:
                    points.extend(metric.data.data_points)
    return points


def _reset_instrument_cache() -> None:
    """
    Clear the module-level histogram cache.

    Each test installs a fresh ``MeterProvider``; without clearing the
    cache, the helpers would keep recording against the previous test's
    provider and the new reader would see nothing.
    """
    telemetry._token_usage_histogram = None  # type: ignore[attr-defined]
    telemetry._operation_duration_histogram = None  # type: ignore[attr-defined]
    telemetry._tool_duration_histogram = None  # type: ignore[attr-defined]


@pytest.fixture
def in_memory_meter_reader() -> Iterator[InMemoryMetricReader]:
    """
    Install a fresh ``MeterProvider`` with an ``InMemoryMetricReader``.

    :yields: The reader the test should query for emitted data points.
    """
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    # Bypass OTel's set-once guard so each test gets a fresh provider.
    otel_metrics._internal._METER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    otel_metrics.set_meter_provider(provider)

    _reset_instrument_cache()

    yield reader

    _reset_instrument_cache()


def test_record_token_usage_metric_emits_input_and_output_points(
    in_memory_meter_reader: InMemoryMetricReader,
) -> None:
    """
    Recording a token usage payload produces one histogram data point per
    token type, each carrying provider, model, and token.type attributes
    so backends can aggregate spend per ``(provider, model, token.type)``.
    """
    telemetry.record_token_usage_metric(
        {"input_tokens": 100, "output_tokens": 50},
        provider="openai",
        model="gpt-5.4",
    )

    points = _data_points(in_memory_meter_reader, "gen_ai.client.token.usage")
    by_token_type = {p.attributes["gen_ai.token.type"]: p for p in points}

    assert set(by_token_type) == {"input", "output"}, (
        f"expected one point per token type, got attribute sets {[p.attributes for p in points]!r}"
    )
    assert by_token_type["input"].sum == 100
    assert by_token_type["output"].sum == 50
    for point in points:
        assert point.attributes["gen_ai.provider.name"] == "openai"
        assert point.attributes["gen_ai.request.model"] == "gpt-5.4"


def test_record_token_usage_metric_skips_missing_keys(
    in_memory_meter_reader: InMemoryMetricReader,
) -> None:
    """
    Missing ``input_tokens`` or ``output_tokens`` keys produce no data
    point for that token type. Absence is meaningful and should not be
    masked with invented zeros.
    """
    telemetry.record_token_usage_metric(
        {"output_tokens": 7},
        provider="anthropic",
        model="claude-sonnet-4",
    )

    points = _data_points(in_memory_meter_reader, "gen_ai.client.token.usage")
    token_types = [p.attributes["gen_ai.token.type"] for p in points]
    assert token_types == ["output"]


def test_record_operation_duration_metric_emits_point(
    in_memory_meter_reader: InMemoryMetricReader,
) -> None:
    """
    Recording a duration produces a single data point on
    ``gen_ai.client.operation.duration`` keyed on provider, model, and
    operation name.
    """
    telemetry.record_operation_duration_metric(
        duration_s=1.25,
        provider="openai",
        model="gpt-5.4",
        operation="chat",
    )

    points = _data_points(in_memory_meter_reader, "gen_ai.client.operation.duration")
    assert len(points) == 1
    point = points[0]
    assert point.sum == pytest.approx(1.25)
    assert point.attributes["gen_ai.provider.name"] == "openai"
    assert point.attributes["gen_ai.request.model"] == "gpt-5.4"
    assert point.attributes["gen_ai.operation.name"] == "chat"


def test_record_tool_duration_metric_emits_point(
    in_memory_meter_reader: InMemoryMetricReader,
) -> None:
    """
    Recording a tool duration produces a data point on
    ``omnigent.tool.duration`` keyed on ``tool.name``.
    """
    telemetry.record_tool_duration_metric(duration_s=0.42, tool_name="web_search")

    points = _data_points(in_memory_meter_reader, "omnigent.tool.duration")
    assert len(points) == 1
    point = points[0]
    assert point.sum == pytest.approx(0.42)
    assert point.attributes["tool.name"] == "web_search"


def test_helpers_noop_when_meter_provider_not_initialized() -> None:
    """
    When ``init`` was not called (or ``OTEL_METRICS_EXPORTER=none``),
    the no-op meter provider is in effect. The helpers must not raise
    so the request hot path stays uninterrupted.
    """
    _reset_instrument_cache()

    # Restore OTel's no-op meter provider so we can prove the helpers
    # stay silent against a provider that produces no metrics.
    from opentelemetry.metrics import NoOpMeterProvider

    otel_metrics._internal._METER_PROVIDER_SET_ONCE._done = False  # type: ignore[attr-defined]
    otel_metrics.set_meter_provider(NoOpMeterProvider())

    telemetry.record_token_usage_metric(
        {"input_tokens": 1, "output_tokens": 2},
        provider="openai",
        model="gpt-5.4",
    )
    telemetry.record_operation_duration_metric(
        duration_s=0.1,
        provider="openai",
        model="gpt-5.4",
        operation="chat",
    )
    telemetry.record_tool_duration_metric(duration_s=0.1, tool_name="t")

    _reset_instrument_cache()


def test_record_token_usage_metric_swallows_exceptions(
    in_memory_meter_reader: InMemoryMetricReader,
) -> None:
    """
    A broken meter does not propagate into the caller. The helper logs
    and moves on so a misconfigured metric pipeline cannot break the
    inner tracing layer.
    """

    # Force the cached instrument to raise on ``record`` so the except
    # branch in the helper fires.
    class _Boom:
        def record(self, *_: Any, **__: Any) -> None:
            raise RuntimeError("boom")

    telemetry._token_usage_histogram = _Boom()  # type: ignore[assignment]
    try:
        telemetry.record_token_usage_metric(
            {"input_tokens": 1, "output_tokens": 2},
            provider="openai",
            model="gpt-5.4",
        )
    finally:
        _reset_instrument_cache()
