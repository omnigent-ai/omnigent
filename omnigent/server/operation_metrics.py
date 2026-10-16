"""Outcome-labelled metrics for the Omnigent server's key operations.

These are the server's service-level indicators. Each completed operation
(session create, sandbox launch, a posted event, ...) is recorded once in the
``omnigent.server.operation.duration`` histogram with two attributes:

* ``operation.name``, e.g. ``"sandbox_launch"``.
* ``outcome``: ``ok``, ``client_error`` or ``server_error``. Only
  ``server_error`` is the server's fault, so an availability objective is
  ``1 - server_error / total`` over the histogram's count.

The HTTP request histogram carries the same ``outcome`` attribute (see
:mod:`omnigent.server.performance_metrics`).

Every recorder is best-effort and a no-op unless
:func:`omnigent.runtime.telemetry.metrics_enabled`.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from functools import lru_cache
from typing import Literal, ParamSpec, TypeVar

from opentelemetry import metrics as otel_metrics

from omnigent.errors import ErrorCode
from omnigent.runtime.telemetry import metrics_enabled
from omnigent.runtime.websocket_metrics import TunnelKind

_logger = logging.getLogger(__name__)

_OTEL_METER_NAME = "omnigent.server.operations"

Outcome = Literal["ok", "client_error", "server_error"]

# Route handler name → operation name, for requests the HTTP middleware records.
HTTP_OPERATIONS: dict[str, str] = {
    "create_session": "session_create",
    "post_event": "event_post",
}

# Seconds, from sub-second API calls to sandbox launches that take minutes.
_DURATION_BUCKETS_S = (0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600, 1800, 3600)

# A session state the client renders as "reconnecting", not a server fault.
_CLIENT_STATE_ERROR_CODES = frozenset({ErrorCode.RUNNER_UNAVAILABLE})

# Scheduled-run failures the server caused; any other code is task or host state.
_SCHEDULED_SERVER_ERROR_CODES = frozenset(
    {
        "launch_failed",
        "session_create_failed",
        "owner_grant_failed",
        "host_registry_unavailable",
        "managed_sandbox_unavailable",
    }
)

_P = ParamSpec("_P")
_T = TypeVar("_T")


def classify_outcome(
    status_code: int | None,
    *,
    failed: bool = False,
    error_code: str | None = None,
) -> Outcome:
    """
    Classify one request or operation result.

    :param status_code: HTTP status, e.g. ``201``; ``None`` when no response
        was produced.
    :param failed: Whether the request raised.
    :param error_code: The ``OmnigentError`` code, when one was raised.
    :returns: ``"ok"``, ``"client_error"`` (4xx, 499, client-state codes) or
        ``"server_error"`` (5xx, exceptions, no response).
    """
    if error_code in _CLIENT_STATE_ERROR_CODES:
        return "client_error"
    if failed or status_code is None or status_code >= 500:
        return "server_error"
    if status_code >= 400:
        return "client_error"
    return "ok"


def scheduled_run_outcome(status: str, error_code: str | None) -> Outcome:
    """
    Classify one recorded scheduled-task run.

    :param status: Run status, ``"running"`` once launched or ``"failed"``.
    :param error_code: Failure code, e.g. ``"launch_failed"``.
    :returns: Outcome of the run.
    """
    if status != "failed":
        return "ok"
    if error_code in _SCHEDULED_SERVER_ERROR_CODES:
        return "server_error"
    return "client_error"


class OperationMetrics:
    """OpenTelemetry instruments for the server's key operations."""

    def __init__(self, meter: otel_metrics.Meter | None = None) -> None:
        """Create the instruments on *meter* (the global provider's by default)."""
        effective_meter = meter or otel_metrics.get_meter(_OTEL_METER_NAME)
        self._operation_duration = effective_meter.create_histogram(
            "omnigent.server.operation.duration",
            unit="s",
            description="Duration of completed server operations, by operation and outcome.",
            explicit_bucket_boundaries_advisory=_DURATION_BUCKETS_S,
        )
        self._errors = effective_meter.create_counter(
            "omnigent.server.errors",
            unit="{error}",
            description="OmnigentError responses by error code.",
        )
        self._tunnels_active = effective_meter.create_up_down_counter(
            "omnigent.server.tunnel.active",
            unit="{connection}",
            description="Host and runner WebSocket tunnel handlers currently running.",
        )
        self._tunnels_closed = effective_meter.create_counter(
            "omnigent.server.tunnel.closed",
            unit="{connection}",
            description="Host and runner WebSocket tunnel handlers that ended.",
        )

    def record_operation(self, name: str, duration_seconds: float, outcome: Outcome) -> None:
        """Record one completed operation."""
        self._operation_duration.record(
            duration_seconds,
            attributes={"operation.name": name, "outcome": outcome},
        )

    def record_error(self, error_code: str) -> None:
        """Count one ``OmnigentError`` response."""
        self._errors.add(1, attributes={"error.code": error_code})

    def record_tunnel_opened(self, kind: TunnelKind) -> None:
        """Count a tunnel handler as started."""
        self._tunnels_active.add(1, attributes={"tunnel.kind": kind})

    def record_tunnel_closed(self, kind: TunnelKind) -> None:
        """Count a tunnel handler as ended."""
        self._tunnels_active.add(-1, attributes={"tunnel.kind": kind})
        self._tunnels_closed.add(1, attributes={"tunnel.kind": kind})


@lru_cache(maxsize=1)
def _default_metrics() -> OperationMetrics:
    """Return the process-wide instruments."""
    return OperationMetrics()


def _best_effort(record: Callable[[OperationMetrics], None]) -> None:
    """Run *record* against the default instruments; never raise."""
    if not metrics_enabled():
        return
    try:
        record(_default_metrics())
    except Exception:  # noqa: BLE001 - metrics must never disrupt serving.
        _logger.debug("failed to record operation metric", exc_info=True)


def record_operation(name: str, duration_seconds: float, outcome: Outcome) -> None:
    """Best-effort record of one completed operation."""
    _best_effort(lambda m: m.record_operation(name, duration_seconds, outcome))


def record_error(error_code: str) -> None:
    """Best-effort count of one ``OmnigentError`` response."""
    _best_effort(lambda m: m.record_error(error_code))


def tracked_tunnel(
    kind: TunnelKind,
) -> Callable[[Callable[_P, Awaitable[None]]], Callable[_P, Awaitable[None]]]:
    """Decorate a tunnel WebSocket endpoint to count it while it runs."""

    def decorate(fn: Callable[_P, Awaitable[None]]) -> Callable[_P, Awaitable[None]]:
        @functools.wraps(fn)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> None:
            _best_effort(lambda m: m.record_tunnel_opened(kind))
            try:
                await fn(*args, **kwargs)
            finally:
                _best_effort(lambda m: m.record_tunnel_closed(kind))

        return wrapper

    return decorate


def _outcome_for_exception(exc: Exception) -> Outcome:
    """Classify an exception by the HTTP status it carries, if any."""
    status_code = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    error_code = getattr(exc, "code", None)
    return classify_outcome(
        status_code if isinstance(status_code, int) else None,
        error_code=str(error_code) if error_code is not None else None,
    )


@contextmanager
def track_operation(name: str) -> Iterator[None]:
    """
    Record the enclosed block as one operation.

    A raised ``HTTPException``/``OmnigentError`` is classified by its status;
    any other exception is a ``server_error``. Cancellation is not recorded.

    :param name: Operation name, e.g. ``"sandbox_launch"``.
    """
    started = time.monotonic()
    try:
        yield
    except Exception as exc:
        record_operation(name, time.monotonic() - started, _outcome_for_exception(exc))
        raise
    record_operation(name, time.monotonic() - started, "ok")


def timed_operation(
    name: str,
) -> Callable[[Callable[_P, Awaitable[_T]]], Callable[_P, Awaitable[_T]]]:
    """Decorate an async function so each call is recorded by :func:`track_operation`."""

    def decorate(fn: Callable[_P, Awaitable[_T]]) -> Callable[_P, Awaitable[_T]]:
        @functools.wraps(fn)
        async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _T:
            with track_operation(name):
                return await fn(*args, **kwargs)

        return wrapper

    return decorate
