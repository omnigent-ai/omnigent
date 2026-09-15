"""Tests for database transaction retry metrics."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest
from opentelemetry.util.types import Attributes

from omnigent.db.metrics import (
    TRANSACTION_RETRIES_METRIC_NAME,
    DatabaseMetrics,
    record_transaction_retry,
)


@dataclass(frozen=True)
class _Record:
    """One recorded counter delta."""

    amount: int | float
    attributes: Attributes


@dataclass
class _Counter:
    """Fake OpenTelemetry counter."""

    records: list[_Record] = field(default_factory=list)

    def add(self, amount: int | float, attributes: Attributes = None) -> None:
        """Record one counter addition."""
        self.records.append(_Record(amount, attributes))


@dataclass
class _Meter:
    """Fake OpenTelemetry meter."""

    counters: dict[str, _Counter] = field(default_factory=dict)

    def create_counter(
        self,
        name: str,
        unit: str = "",
        description: str = "",
    ) -> _Counter:
        """Create a named fake counter."""
        del unit, description
        counter = _Counter()
        self.counters[name] = counter
        return counter


def test_database_metrics_record_bounded_retry_attributes() -> None:
    """Retry metrics carry only stable, bounded dimensions."""
    meter = _Meter()
    metrics = DatabaseMetrics(meter)

    metrics.record_transaction_retry("omnigent.message.append", "scheduled")
    metrics.record_transaction_retry("omnigent.message.append", "exhausted")

    assert meter.counters[TRANSACTION_RETRIES_METRIC_NAME].records == [
        _Record(
            1,
            {
                "db.system": "cockroachdb",
                "db.operation": "omnigent.message.append",
                "retry.outcome": "scheduled",
            },
        ),
        _Record(
            1,
            {
                "db.system": "cockroachdb",
                "db.operation": "omnigent.message.append",
                "retry.outcome": "exhausted",
            },
        ),
    ]


def test_public_recorder_respects_telemetry_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The public helper does nothing unless telemetry is enabled."""
    monkeypatch.delenv("OMNIGENT_TELEMETRY_ENABLED", raising=False)
    monkeypatch.setattr(
        "omnigent.db.metrics._default_metrics",
        lambda: pytest.fail("metrics should not be initialized"),
    )

    record_transaction_retry("omnigent.message.append", "scheduled")


def test_public_recorder_is_best_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    """Metrics failures never replace the database exception or result."""
    monkeypatch.setenv("OMNIGENT_TELEMETRY_ENABLED", "true")

    def fail() -> DatabaseMetrics:
        raise RuntimeError("collector unavailable")

    monkeypatch.setattr("omnigent.db.metrics._default_metrics", fail)

    record_transaction_retry("omnigent.message.append", "exhausted")
