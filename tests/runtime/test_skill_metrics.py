"""Tests for skill-execution metrics (``omnigent.runtime.skill_metrics``)."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from opentelemetry.util.types import Attributes

from omnigent.runtime import skill_metrics
from omnigent.runtime.skill_metrics import (
    EXECUTION_DURATION_METRIC_NAME,
    INVOCATIONS_METRIC_NAME,
    TOOL_CALLS_METRIC_NAME,
    SkillMetrics,
)


@dataclass(frozen=True)
class _Record:
    """One recorded counter/histogram value."""

    amount: int | float
    attributes: Attributes


@dataclass
class _Counter:
    """Fake OpenTelemetry counter."""

    records: list[_Record] = field(default_factory=list)

    def add(self, amount: int | float, attributes: Attributes = None) -> None:
        self.records.append(_Record(amount, attributes))


@dataclass
class _Histogram:
    """Fake OpenTelemetry histogram."""

    records: list[_Record] = field(default_factory=list)

    def record(self, amount: int | float, attributes: Attributes = None) -> None:
        self.records.append(_Record(amount, attributes))


@dataclass
class _Meter:
    """Fake OpenTelemetry meter."""

    counters: dict[str, _Counter] = field(default_factory=dict)
    histograms: dict[str, _Histogram] = field(default_factory=dict)

    def create_counter(self, name: str, unit: str = "", description: str = "") -> _Counter:
        del unit, description
        counter = _Counter()
        self.counters[name] = counter
        return counter

    def create_histogram(self, name: str, unit: str = "", description: str = "") -> _Histogram:
        del unit, description
        histogram = _Histogram()
        self.histograms[name] = histogram
        return histogram


def test_records_invocation_duration_and_tool_calls() -> None:
    """Each recorder writes the expected value and low-cardinality labels."""
    meter = _Meter()
    metrics = SkillMetrics(meter=meter)

    metrics.record_invocation("code-review", "success")
    metrics.record_execution_duration("code-review", 1234.5)
    metrics.record_tool_call("code-review", "Bash")

    assert meter.counters[INVOCATIONS_METRIC_NAME].records == [
        _Record(1, {"skill.name": "code-review", "outcome": "success"})
    ]
    assert meter.histograms[EXECUTION_DURATION_METRIC_NAME].records == [
        _Record(1234.5, {"skill.name": "code-review"})
    ]
    assert meter.counters[TOOL_CALLS_METRIC_NAME].records == [
        _Record(1, {"skill.name": "code-review", "tool.name": "Bash"})
    ]


def test_module_recorders_no_op_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``@telemetry_guarded`` module functions do nothing (and never raise)
    when telemetry is disabled."""
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "false")
    meter = _Meter()
    instrument_lookups: list[None] = []

    def _metrics() -> SkillMetrics:
        instrument_lookups.append(None)
        return SkillMetrics(meter=meter)

    monkeypatch.setattr(skill_metrics, "_default_metrics", _metrics)

    skill_metrics.record_skill_invocation("x", "success")
    skill_metrics.record_skill_execution_duration("x", 1.0)
    skill_metrics.record_skill_tool_call("x", "Bash")

    assert instrument_lookups == []
    assert meter.counters == {}
    assert meter.histograms == {}


def test_module_recorders_emit_when_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    """When telemetry is on, module recorders write through to the instruments."""
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "1")
    meter = _Meter()
    monkeypatch.setattr(skill_metrics, "_default_metrics", lambda: SkillMetrics(meter=meter))

    skill_metrics.record_skill_invocation("code-review", "error")

    assert meter.counters[INVOCATIONS_METRIC_NAME].records == [
        _Record(1, {"skill.name": "code-review", "outcome": "error"})
    ]
