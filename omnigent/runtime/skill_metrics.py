"""OpenTelemetry metrics for skill execution.

Labels are low-cardinality (skill and tool names only, never content). Timing
and outcome are turn-scoped: a skill runs from its Skill / load_skill call to
the end of the turn, since there is no skill-end signal.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Protocol

from opentelemetry import metrics as otel_metrics
from opentelemetry.util.types import Attributes

from omnigent.runtime.telemetry import telemetry_guarded

_OTEL_METER_NAME = "omnigent.skill"
INVOCATIONS_METRIC_NAME = "omnigent.skill.invocations"
EXECUTION_DURATION_METRIC_NAME = "omnigent.skill.execution.duration"
TOOL_CALLS_METRIC_NAME = "omnigent.skill.tool_calls"


class _CounterLike(Protocol):
    """Subset of the OpenTelemetry counter API used here."""

    def add(self, amount: int | float, attributes: Attributes = None) -> None:
        """Add a value with optional metric attributes."""


class _HistogramLike(Protocol):
    """Subset of the OpenTelemetry histogram API used here."""

    def record(self, amount: int | float, attributes: Attributes = None) -> None:
        """Record a value with optional metric attributes."""


class _MeterLike(Protocol):
    """Subset of the OpenTelemetry meter API used here."""

    def create_counter(self, name: str, unit: str = "", description: str = "") -> _CounterLike:
        """Create a monotonic counter."""

    def create_histogram(self, name: str, unit: str = "", description: str = "") -> _HistogramLike:
        """Create a histogram."""


class SkillMetrics:
    """Record bounded skill-execution outcomes, durations, and tool usage."""

    def __init__(self, meter: _MeterLike | None = None) -> None:
        """Create the skill metric instruments."""
        effective_meter = meter or otel_metrics.get_meter(_OTEL_METER_NAME)
        self._invocations = effective_meter.create_counter(
            INVOCATIONS_METRIC_NAME,
            unit="{invocation}",
            description="Skill invocations, labelled by turn-scoped outcome.",
        )
        self._execution_duration = effective_meter.create_histogram(
            EXECUTION_DURATION_METRIC_NAME,
            unit="ms",
            description="Skill execution duration (skill call to turn end, approximate).",
        )
        self._tool_calls = effective_meter.create_counter(
            TOOL_CALLS_METRIC_NAME,
            unit="{call}",
            description="Tool calls made while a skill was the active skill.",
        )

    def record_invocation(self, skill_name: str, outcome: str) -> None:
        """Record one skill invocation with its turn outcome."""
        self._invocations.add(1, attributes={"skill.name": skill_name, "outcome": outcome})

    def record_execution_duration(self, skill_name: str, duration_ms: float) -> None:
        """Record a skill's execution duration in milliseconds."""
        self._execution_duration.record(duration_ms, attributes={"skill.name": skill_name})

    def record_tool_call(self, skill_name: str, tool_name: str) -> None:
        """Record one tool call made while *skill_name* was active."""
        self._tool_calls.add(1, attributes={"skill.name": skill_name, "tool.name": tool_name})


@lru_cache(maxsize=1)
def _default_metrics() -> SkillMetrics:
    """Return the process-wide skill metric instruments."""
    return SkillMetrics()


@telemetry_guarded
def record_skill_invocation(skill_name: str, outcome: str) -> None:
    """Record a skill invocation and its turn outcome."""
    _default_metrics().record_invocation(skill_name, outcome)


@telemetry_guarded
def record_skill_execution_duration(skill_name: str, duration_ms: float) -> None:
    """Record a skill's execution duration in milliseconds."""
    _default_metrics().record_execution_duration(skill_name, duration_ms)


@telemetry_guarded
def record_skill_tool_call(skill_name: str, tool_name: str) -> None:
    """Record a tool call made while a skill was active."""
    _default_metrics().record_tool_call(skill_name, tool_name)
