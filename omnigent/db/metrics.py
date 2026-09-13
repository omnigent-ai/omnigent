"""OpenTelemetry metrics for database transaction retries."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal, Protocol

from opentelemetry import metrics as otel_metrics
from opentelemetry.util.types import Attributes

from omnigent.runtime.telemetry import telemetry_enabled

_logger = logging.getLogger(__name__)

_OTEL_METER_NAME = "omnigent.database"
TRANSACTION_RETRIES_METRIC_NAME = "omnigent.database.transaction.retries"

RetryOutcome = Literal["scheduled", "exhausted"]


class _CounterLike(Protocol):
    """Subset of the OpenTelemetry counter API used here."""

    def add(self, amount: int | float, attributes: Attributes = None) -> None:
        """Add a value with optional metric attributes."""


class _MeterLike(Protocol):
    """Subset of the OpenTelemetry meter API used here."""

    def create_counter(
        self,
        name: str,
        unit: str = "",
        description: str = "",
    ) -> _CounterLike:
        """Create a monotonic counter."""


class DatabaseMetrics:
    """Record bounded database transaction retry outcomes."""

    def __init__(self, meter: _MeterLike | None = None) -> None:
        """Create the transaction retry counter."""
        effective_meter = meter or otel_metrics.get_meter(_OTEL_METER_NAME)
        self._transaction_retries = effective_meter.create_counter(
            TRANSACTION_RETRIES_METRIC_NAME,
            unit="{retry}",
            description="CockroachDB transaction retry outcomes.",
        )

    def record_transaction_retry(self, operation: str, outcome: RetryOutcome) -> None:
        """Record one scheduled or exhausted transaction retry."""
        self._transaction_retries.add(
            1,
            attributes={
                "db.system": "cockroachdb",
                "db.operation": operation,
                "retry.outcome": outcome,
            },
        )


@lru_cache(maxsize=1)
def _default_metrics() -> DatabaseMetrics:
    """Return the process-wide database metric instruments."""
    return DatabaseMetrics()


def record_transaction_retry(operation: str, outcome: RetryOutcome) -> None:
    """Best-effort record of a CockroachDB transaction retry outcome."""
    if not telemetry_enabled():
        return
    try:
        _default_metrics().record_transaction_retry(operation, outcome)
    except Exception:  # noqa: BLE001 - metrics must not disrupt database work.
        _logger.debug("failed to record database retry metric", exc_info=True)
