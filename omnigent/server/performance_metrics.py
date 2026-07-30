"""Lightweight server performance metrics for the FastAPI app."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import os
import sys
import time

try:
    import resource  # POSIX-only; absent on Windows.
except ImportError:  # pragma: no cover - exercised only on Windows
    resource = None  # type: ignore[assignment]
from collections import deque
from dataclasses import dataclass
from threading import Lock
from typing import Protocol

from opentelemetry import metrics as otel_metrics
from opentelemetry.util.types import Attributes
from uvicorn.logging import AccessFormatter

from omnigent.process_logging import log_record_display_fields

_DEFAULT_WINDOWS_SECONDS = (1.0, 10.0, 30.0)
_BYTES_PER_MIB = 1024 * 1024
_OTEL_METER_NAME = "omnigent.server.performance"
_REQUEST_DURATION_CONTEXT: contextvars.ContextVar[float | None] = contextvars.ContextVar(
    "omnigent_request_duration_seconds",
    default=None,
)
_REQUEST_ID_CONTEXT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "omnigent_request_id",
    default=None,
)
_REQUEST_USER_AGENT_CONTEXT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "omnigent_request_user_agent",
    default=None,
)
_REQUEST_SESSION_ID_CONTEXT: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "omnigent_request_session_id",
    default=None,
)


class FloatSampler(Protocol):
    """
    Protocol for dependency-injected float samplers.
    """

    def __call__(self) -> float:
        """
        Return the sampled float value.

        :returns: A sampled float value.
        """
        ...


class RssSampler(Protocol):
    """
    Protocol for dependency-injected resident-memory samplers.
    """

    def __call__(self) -> int:
        """
        Return resident memory in bytes.

        :returns: Resident memory in bytes.
        """
        ...


class LoadAverageSampler(Protocol):
    """
    Protocol for dependency-injected load-average samplers.
    """

    def __call__(self) -> SystemLoadAverage | None:
        """
        Return system load averages.

        :returns: System load averages, or ``None`` when unavailable.
        """
        ...


class GaugeInstrument(Protocol):
    """
    Protocol for OpenTelemetry synchronous gauge instruments.
    """

    def set(self, amount: int | float, attributes: Attributes = None) -> None:
        """
        Set the gauge to a point-in-time value.

        :param amount: Gauge value to record.
        :param attributes: Optional OpenTelemetry metric attributes.
        """
        ...


class CounterInstrument(Protocol):
    """
    Protocol for OpenTelemetry counter instruments.
    """

    def add(self, amount: int | float, attributes: Attributes = None) -> None:
        """
        Add a non-negative value to the counter.

        :param amount: Counter delta to record.
        :param attributes: Optional OpenTelemetry metric attributes.
        """
        ...


class HistogramInstrument(Protocol):
    """
    Protocol for OpenTelemetry histogram instruments.
    """

    def record(self, amount: int | float, attributes: Attributes = None) -> None:
        """
        Record one histogram sample.

        :param amount: Histogram sample value.
        :param attributes: Optional OpenTelemetry metric attributes.
        """
        ...


class MeterLike(Protocol):
    """
    Protocol for the OpenTelemetry meter methods used by this module.
    """

    def create_counter(
        self,
        name: str,
        unit: str = "",
        description: str = "",
    ) -> CounterInstrument:
        """
        Create a monotonic counter instrument.

        :param name: Metric name, e.g.
            ``"omnigent.server.http.requests.started"``.
        :param unit: UCUM unit string, e.g. ``"{request}"``.
        :param description: Human-readable metric description.
        :returns: OpenTelemetry counter instrument.
        """
        ...

    def create_gauge(
        self,
        name: str,
        unit: str = "",
        description: str = "",
    ) -> GaugeInstrument:
        """
        Create a synchronous gauge instrument.

        :param name: Metric name, e.g.
            ``"omnigent.server.http.requests.in_flight"``.
        :param unit: UCUM unit string, e.g. ``"{request}"``.
        :param description: Human-readable metric description.
        :returns: OpenTelemetry gauge instrument.
        """
        ...

    def create_histogram(
        self,
        name: str,
        unit: str = "",
        description: str = "",
    ) -> HistogramInstrument:
        """
        Create a histogram instrument.

        :param name: Metric name, e.g.
            ``"omnigent.server.http.request.duration"``.
        :param unit: UCUM unit string, e.g. ``"s"``.
        :param description: Human-readable metric description.
        :returns: OpenTelemetry histogram instrument.
        """
        ...


def _sanitize_access_log_value(value: str) -> str:
    """
    Make a client-controlled string safe to embed in an access-log line.

    The ``User-Agent`` header and the session ID parsed from the URL path
    are both attacker-controlled. Replaces control characters (CR, LF,
    NUL, ANSI escape sequences, ...) and the double-quote delimiter with
    ``?`` so a crafted value cannot forge additional log records, corrupt
    the quoted ``ua`` field, or smuggle terminal escapes into log viewers
    (CWE-117).

    :param value: Raw, untrusted string to embed in the access log.
    :returns: Value containing only printable, non-quote characters.
    """
    return "".join(char if char.isprintable() and char != '"' else "?" for char in value)


class RequestDurationAccessFormatter(AccessFormatter):
    """
    Uvicorn access formatter that appends Omnigent request duration.

    Uvicorn owns the actual access log emission. The application
    middleware records request duration in a context variable, and
    Uvicorn formats the access record later in the same request task.
    The suffix intentionally stays terse, e.g. ``"2.5ms"``, so the
    line keeps Uvicorn's standard access-log shape.
    """

    _MAX_USER_AGENT_LENGTH = 80

    def format(self, record: logging.LogRecord) -> str:
        """Apply Omnigent's standard source and level display fields."""
        fmt = getattr(self._style, "_fmt", "")
        with log_record_display_fields(
            record,
            use_colors=self.use_colors,
            format_level="%(levelprefix)" not in fmt,
        ):
            return super().format(record)

    def formatMessage(self, record: logging.LogRecord) -> str:
        """
        Format one Uvicorn access record with request context.

        Appends duration, request ID, User-Agent, and session ID when
        available.  Example output::

            GET /v1/sessions/conv_abc HTTP/1.1 200 2.5ms rid=a1b2c3d4 ua="httpx/0.27" sid=conv_abc

        :param record: Logging record emitted by Uvicorn's access
            logger.
        :returns: Enriched access message.
        """
        message = super().formatMessage(record)
        parts: list[str] = [message]
        try:
            duration_seconds = _REQUEST_DURATION_CONTEXT.get()
            if duration_seconds is not None:
                parts.append(f"{duration_seconds * 1000.0:.1f}ms")

            request_id = _REQUEST_ID_CONTEXT.get()
            if request_id is not None:
                parts.append(f"rid={request_id}")

            user_agent = _REQUEST_USER_AGENT_CONTEXT.get()
            if user_agent is not None:
                truncated = user_agent[: self._MAX_USER_AGENT_LENGTH]
                parts.append(f'ua="{_sanitize_access_log_value(truncated)}"')

            session_id = _REQUEST_SESSION_ID_CONTEXT.get()
            if session_id is not None:
                parts.append(f"sid={_sanitize_access_log_value(session_id)}")
        finally:
            _REQUEST_DURATION_CONTEXT.set(None)
            _REQUEST_ID_CONTEXT.set(None)
            _REQUEST_USER_AGENT_CONTEXT.set(None)
            _REQUEST_SESSION_ID_CONTEXT.set(None)
        return " ".join(parts)


@dataclass(frozen=True)
class SystemLoadAverage:
    """
    System load averages for standard Unix windows.

    :param one_minute: One-minute system load average.
    :param five_minutes: Five-minute system load average.
    :param fifteen_minutes: Fifteen-minute system load average.
    """

    one_minute: float
    five_minutes: float
    fifteen_minutes: float


@dataclass
class _CounterState:
    """
    Last emitted cumulative counter values for OTEL delta publishing.

    :param total_started: Last ``total_started`` snapshot value.
    :param total_completed: Last ``total_completed`` snapshot value.
    :param total_failed: Last ``total_failed`` snapshot value.
    """

    total_started: int = 0
    total_completed: int = 0
    total_failed: int = 0


@dataclass(frozen=True)
class _RequestOtelInstruments:
    """
    OpenTelemetry instruments for HTTP and WebSocket request state.

    :param started: Monotonic counter for started HTTP requests.
    :param completed: Monotonic counter for completed HTTP requests.
    :param failed: Monotonic counter for failed HTTP requests.
    :param duration: Histogram for completed HTTP request durations.
    :param in_flight: Gauge for currently processing HTTP requests.
    :param last_1s: Gauge for requests started in the last second.
    :param last_10s: Gauge for requests started in the last ten
        seconds.
    :param last_30s: Gauge for requests started in the last thirty
        seconds.
    :param active_websockets: Gauge for currently open accepted
        WebSocket connections.
    """

    started: CounterInstrument
    completed: CounterInstrument
    failed: CounterInstrument
    duration: HistogramInstrument
    in_flight: GaugeInstrument
    last_1s: GaugeInstrument
    last_10s: GaugeInstrument
    last_30s: GaugeInstrument
    active_websockets: GaugeInstrument


@dataclass(frozen=True)
class _ProcessingOtelInstruments:
    """
    OpenTelemetry gauges for HTTP request processing durations.

    :param avg_ms: Gauge for all-time average request processing
        duration in milliseconds.
    :param max_ms: Gauge for all-time maximum request processing
        duration in milliseconds.
    :param avg_1s_ms: Gauge for one-second rolling average request
        processing duration in milliseconds.
    :param avg_10s_ms: Gauge for ten-second rolling average request
        processing duration in milliseconds.
    :param avg_30s_ms: Gauge for thirty-second rolling average request
        processing duration in milliseconds.
    """

    avg_ms: GaugeInstrument
    max_ms: GaugeInstrument
    avg_1s_ms: GaugeInstrument
    avg_10s_ms: GaugeInstrument
    avg_30s_ms: GaugeInstrument


@dataclass(frozen=True)
class _ResourceOtelInstruments:
    """
    OpenTelemetry gauges for process and system resource state.

    :param cpu_percent: Gauge for process CPU percent.
    :param load_1m: Gauge for one-minute system load average.
    :param load_5m: Gauge for five-minute system load average.
    :param load_15m: Gauge for fifteen-minute system load average.
    :param rss_bytes: Gauge for resident memory bytes.
    """

    cpu_percent: GaugeInstrument
    load_1m: GaugeInstrument
    load_5m: GaugeInstrument
    load_15m: GaugeInstrument
    rss_bytes: GaugeInstrument


@dataclass(frozen=True)
class CompletedRequestTiming:
    """
    Timing for one completed HTTP request.

    :param completed_at: Monotonic timestamp when the request left the
        server.
    :param duration_seconds: Request processing duration in seconds.
    """

    completed_at: float
    duration_seconds: float


@dataclass(frozen=True)
class RequestMetricValues:
    """
    Request metrics copied while holding the tracker lock.

    :param in_flight: HTTP requests currently executing.
    :param total_started: Total HTTP requests started since process
        start.
    :param total_completed: Total HTTP requests completed since
        process start.
    :param total_failed: Total HTTP requests failed since process
        start.
    :param requests_last_1s: Requests started in the last second.
    :param requests_last_10s: Requests started in the last ten
        seconds.
    :param requests_last_30s: Requests started in the last thirty
        seconds.
    :param active_websockets: Accepted WebSocket connections currently
        open in the ASGI app.
    :param request_processing_avg_ms: All-time average request
        processing duration in milliseconds.
    :param request_processing_max_ms: All-time maximum request
        processing duration in milliseconds.
    :param request_processing_avg_1s_ms: Average processing duration
        for requests completed in the last second.
    :param request_processing_avg_10s_ms: Average processing duration
        for requests completed in the last ten seconds.
    :param request_processing_avg_30s_ms: Average processing duration
        for requests completed in the last thirty seconds.
    :param process_cpu_percent: Process CPU percent since the prior
        snapshot.
    """

    in_flight: int
    total_started: int
    total_completed: int
    total_failed: int
    requests_last_1s: int
    requests_last_10s: int
    requests_last_30s: int
    active_websockets: int
    request_processing_avg_ms: float
    request_processing_max_ms: float
    request_processing_avg_1s_ms: float
    request_processing_avg_10s_ms: float
    request_processing_avg_30s_ms: float
    process_cpu_percent: float


@dataclass(frozen=True)
class ServerMetricsSnapshot:
    """
    Point-in-time server performance counters.

    :param in_flight: HTTP requests currently executing in the
        FastAPI app.
    :param total_started: Total HTTP requests accepted by the
        metrics middleware since process start.
    :param total_completed: Total HTTP requests that left the
        metrics middleware since process start.
    :param total_failed: Total HTTP requests that raised or returned
        a 5xx status since process start.
    :param requests_last_1s: HTTP requests started in the last
        second.
    :param requests_last_10s: HTTP requests started in the last ten
        seconds.
    :param requests_last_30s: HTTP requests started in the last
        thirty seconds.
    :param active_websockets: Accepted WebSocket connections currently
        open in the FastAPI app.
    :param request_processing_avg_ms: Average processing duration for
        completed HTTP requests since process start, in milliseconds.
    :param request_processing_max_ms: Maximum processing duration for
        completed HTTP requests since process start, in milliseconds.
    :param request_processing_avg_1s_ms: Average processing duration
        for requests completed in the last second, in milliseconds.
    :param request_processing_avg_10s_ms: Average processing duration
        for requests completed in the last ten seconds, in
        milliseconds.
    :param request_processing_avg_30s_ms: Average processing duration
        for requests completed in the last thirty seconds, in
        milliseconds.
    :param process_cpu_percent: Process CPU use since the previous
        snapshot, expressed as ``process_cpu_seconds / wall_seconds *
        100``. The first snapshot reports ``0.0``.
    :param load_average_1m: System load average over one minute, or
        ``None`` when the platform does not expose load averages.
    :param load_average_5m: System load average over five minutes, or
        ``None`` when the platform does not expose load averages.
    :param load_average_15m: System load average over fifteen minutes,
        or ``None`` when the platform does not expose load averages.
    :param rss_bytes: Resident set size in bytes. On Linux this is
        current RSS from ``/proc/self/status``; elsewhere it falls
        back to ``resource.getrusage().ru_maxrss``.
    """

    in_flight: int
    total_started: int
    total_completed: int
    total_failed: int
    requests_last_1s: int
    requests_last_10s: int
    requests_last_30s: int
    active_websockets: int
    request_processing_avg_ms: float
    request_processing_max_ms: float
    request_processing_avg_1s_ms: float
    request_processing_avg_10s_ms: float
    request_processing_avg_30s_ms: float
    process_cpu_percent: float
    load_average_1m: float | None
    load_average_5m: float | None
    load_average_15m: float | None
    rss_bytes: int

    @property
    def rss_mib(self) -> float:
        """
        Return resident memory as mebibytes.

        :returns: ``rss_bytes`` divided by 1024².
        """
        return self.rss_bytes / _BYTES_PER_MIB


class ServerPerformanceMetrics:
    """
    Process-local metrics tracker for HTTP server requests.

    The tracker is intentionally in-memory and dependency-free. It
    records request start timestamps for rolling-window request
    counters, maintains current in-flight request count, and samples
    process/system resource counters when a snapshot is taken.

    :param clock: Monotonic wall-clock function, e.g.
        ``time.monotonic``. Tests pass a deterministic fake clock.
    :param process_time_fn: Process CPU clock function, e.g.
        ``time.process_time``.
    :param rss_bytes_fn: Function that returns resident memory in
        bytes.
    :param load_avg_fn: Function that returns the one-, five-, and
        fifteen-minute load averages.
    """

    def __init__(
        self,
        *,
        clock: FloatSampler = time.monotonic,
        process_time_fn: FloatSampler = time.process_time,
        rss_bytes_fn: RssSampler | None = None,
        load_avg_fn: LoadAverageSampler | None = None,
    ) -> None:
        """
        Initialize the metrics tracker.

        :param clock: Monotonic wall-clock function, e.g.
            ``time.monotonic``.
        :param process_time_fn: Process CPU clock function, e.g.
            ``time.process_time``.
        :param rss_bytes_fn: Optional resident memory sampler. ``None``
            uses the module's stdlib sampler.
        :param load_avg_fn: Optional system load sampler. ``None``
            uses ``os.getloadavg`` when available.
        """
        self._clock = clock
        self._process_time_fn = process_time_fn
        self._rss_bytes_fn = rss_bytes_fn or _current_rss_bytes
        self._load_avg_fn = load_avg_fn or _load_average
        self._lock = Lock()
        self._request_starts: deque[float] = deque()
        self._request_timings: deque[CompletedRequestTiming] = deque()
        self._in_flight = 0
        self._total_started = 0
        self._total_completed = 0
        self._total_failed = 0
        self._active_websockets = 0
        self._total_processing_seconds = 0.0
        self._max_processing_seconds = 0.0
        self._route_counts: dict[str, int] = {}
        self._last_snapshot_wall = clock()
        self._last_snapshot_cpu = process_time_fn()

    def request_started(self) -> float:
        """
        Record that one HTTP request entered the server.

        :returns: Monotonic start timestamp to pass to
            :meth:`request_finished`.
        """
        now = self._clock()
        with self._lock:
            self._request_starts.append(now)
            self._in_flight += 1
            self._total_started += 1
            self._prune_locked(now)
        return now

    def request_finished(self, *, started_at: float, failed: bool = False) -> float:
        """
        Record that one HTTP request left the server.

        :param started_at: Monotonic start timestamp returned by
            :meth:`request_started`.
        :param failed: Whether the request failed, e.g. raised an
            exception or returned a 5xx response.
        :returns: Request processing duration in seconds.
        """
        now = self._clock()
        duration_seconds = max(0.0, now - started_at)
        with self._lock:
            self._in_flight = max(0, self._in_flight - 1)
            self._total_completed += 1
            if failed:
                self._total_failed += 1
            self._total_processing_seconds += duration_seconds
            self._max_processing_seconds = max(
                self._max_processing_seconds,
                duration_seconds,
            )
            self._request_timings.append(
                CompletedRequestTiming(
                    completed_at=now,
                    duration_seconds=duration_seconds,
                )
            )
            self._prune_locked(now)
        return duration_seconds

    def record_route(self, method: str, route: str) -> None:
        """
        Increment the per-route request counter for ``"METHOD route"``.

        The route is the low-cardinality FastAPI template (e.g.
        ``"/v1/sessions/{session_id}"``), so the key space is bounded by the
        number of registered routes and the map stays small. Used only for
        offline analysis (the benchmark harness's per-journey request
        breakdown); the aggregate counters above drive live metrics.

        :param method: HTTP request method, e.g. ``"GET"``.
        :param route: Matched route template, e.g. ``"/v1/sessions"``.
        """
        key = f"{method} {route}"
        with self._lock:
            self._route_counts[key] = self._route_counts.get(key, 0) + 1

    def route_counts(self) -> dict[str, int]:
        """
        Return a copy of the cumulative per-route request counts.

        :returns: ``"METHOD route"`` mapped to the number of requests handled
            since process start.
        """
        with self._lock:
            return dict(self._route_counts)

    def websocket_connected(self) -> None:
        """
        Record that one WebSocket connection was accepted.
        """
        with self._lock:
            self._active_websockets += 1

    def websocket_disconnected(self) -> None:
        """
        Record that one accepted WebSocket connection closed.
        """
        with self._lock:
            self._active_websockets = max(0, self._active_websockets - 1)

    def snapshot(self) -> ServerMetricsSnapshot:
        """
        Capture request counters and process resource usage.

        :returns: A point-in-time immutable metrics snapshot.
        """
        now = self._clock()
        process_cpu = self._process_time_fn()
        with self._lock:
            self._prune_locked(now)
            request_values = self._request_metric_values_locked(now, process_cpu)

        load = self._load_avg_fn()
        if load is None:
            load_average_1m = None
            load_average_5m = None
            load_average_15m = None
        else:
            load_average_1m = load.one_minute
            load_average_5m = load.five_minutes
            load_average_15m = load.fifteen_minutes
        return ServerMetricsSnapshot(
            in_flight=request_values.in_flight,
            total_started=request_values.total_started,
            total_completed=request_values.total_completed,
            total_failed=request_values.total_failed,
            requests_last_1s=request_values.requests_last_1s,
            requests_last_10s=request_values.requests_last_10s,
            requests_last_30s=request_values.requests_last_30s,
            active_websockets=request_values.active_websockets,
            request_processing_avg_ms=request_values.request_processing_avg_ms,
            request_processing_max_ms=request_values.request_processing_max_ms,
            request_processing_avg_1s_ms=request_values.request_processing_avg_1s_ms,
            request_processing_avg_10s_ms=request_values.request_processing_avg_10s_ms,
            request_processing_avg_30s_ms=request_values.request_processing_avg_30s_ms,
            process_cpu_percent=request_values.process_cpu_percent,
            load_average_1m=load_average_1m,
            load_average_5m=load_average_5m,
            load_average_15m=load_average_15m,
            rss_bytes=self._rss_bytes_fn(),
        )

    def _request_metric_values_locked(
        self,
        now: float,
        process_cpu: float,
    ) -> RequestMetricValues:
        """
        Copy request counters and timing values under the tracker lock.

        :param now: Current monotonic time in seconds.
        :param process_cpu: Current process CPU time in seconds.
        :returns: Request metrics that do not require further lock
            access.
        """
        return RequestMetricValues(
            in_flight=self._in_flight,
            total_started=self._total_started,
            total_completed=self._total_completed,
            total_failed=self._total_failed,
            requests_last_1s=self._count_since_locked(now - 1.0),
            requests_last_10s=self._count_since_locked(now - 10.0),
            requests_last_30s=self._count_since_locked(now - 30.0),
            active_websockets=self._active_websockets,
            request_processing_avg_ms=self._overall_processing_avg_ms_locked(),
            request_processing_max_ms=self._max_processing_seconds * 1000.0,
            request_processing_avg_1s_ms=self._processing_avg_since_locked(now - 1.0),
            request_processing_avg_10s_ms=self._processing_avg_since_locked(now - 10.0),
            request_processing_avg_30s_ms=self._processing_avg_since_locked(now - 30.0),
            process_cpu_percent=self._cpu_percent_locked(now, process_cpu),
        )

    def _prune_locked(self, now: float) -> None:
        """
        Drop request starts older than the largest reporting window.

        :param now: Current monotonic time in seconds.
        """
        cutoff = now - max(_DEFAULT_WINDOWS_SECONDS)
        while self._request_starts and self._request_starts[0] < cutoff:
            self._request_starts.popleft()
        while self._request_timings and self._request_timings[0].completed_at < cutoff:
            self._request_timings.popleft()

    def _count_since_locked(self, cutoff: float) -> int:
        """
        Count request starts newer than ``cutoff``.

        :param cutoff: Exclusive lower-bound monotonic timestamp.
        :returns: Number of retained request starts at or after
            ``cutoff``.
        """
        return sum(1 for started_at in self._request_starts if started_at >= cutoff)

    def _overall_processing_avg_ms_locked(self) -> float:
        """
        Return all-time average completed request processing time.

        :returns: Average processing duration in milliseconds, or
            ``0.0`` when no request has completed.
        """
        if self._total_completed == 0:
            return 0.0
        return (self._total_processing_seconds / self._total_completed) * 1000.0

    def _processing_avg_since_locked(self, cutoff: float) -> float:
        """
        Return average processing time for recently completed requests.

        :param cutoff: Exclusive lower-bound completion timestamp.
        :returns: Average processing duration in milliseconds, or
            ``0.0`` when no retained request completed after
            ``cutoff``.
        """
        total_seconds = 0.0
        count = 0
        for timing in self._request_timings:
            if timing.completed_at >= cutoff:
                total_seconds += timing.duration_seconds
                count += 1
        if count == 0:
            return 0.0
        return (total_seconds / count) * 1000.0

    def _cpu_percent_locked(self, now: float, process_cpu: float) -> float:
        """
        Compute CPU percent since the previous snapshot.

        :param now: Current monotonic time in seconds.
        :param process_cpu: Current process CPU time in seconds.
        :returns: Percent CPU used since the prior snapshot.
        """
        wall_delta = max(0.0, now - self._last_snapshot_wall)
        cpu_delta = max(0.0, process_cpu - self._last_snapshot_cpu)
        self._last_snapshot_wall = now
        self._last_snapshot_cpu = process_cpu
        if wall_delta <= 0:
            return 0.0
        return (cpu_delta / wall_delta) * 100.0


def _create_request_otel_instruments(meter: MeterLike) -> _RequestOtelInstruments:
    """
    Create OpenTelemetry instruments for request and WebSocket state.

    :param meter: Meter used to create instruments.
    :returns: Grouped request instruments.
    """
    return _RequestOtelInstruments(
        started=meter.create_counter(
            "omnigent.server.http.requests.started",
            unit="{request}",
            description="HTTP requests started by the Omnigent server.",
        ),
        completed=meter.create_counter(
            "omnigent.server.http.requests.completed",
            unit="{request}",
            description="HTTP requests completed by the Omnigent server.",
        ),
        failed=meter.create_counter(
            "omnigent.server.http.requests.failed",
            unit="{request}",
            description="HTTP requests failed by exception or 5xx status.",
        ),
        duration=meter.create_histogram(
            "omnigent.server.http.request.duration",
            unit="s",
            description="HTTP request processing duration.",
        ),
        in_flight=meter.create_gauge(
            "omnigent.server.http.requests.in_flight",
            unit="{request}",
            description="HTTP requests currently being processed.",
        ),
        last_1s=meter.create_gauge(
            "omnigent.server.http.requests.last_1s",
            unit="{request}",
            description="HTTP requests started in the last second.",
        ),
        last_10s=meter.create_gauge(
            "omnigent.server.http.requests.last_10s",
            unit="{request}",
            description="HTTP requests started in the last ten seconds.",
        ),
        last_30s=meter.create_gauge(
            "omnigent.server.http.requests.last_30s",
            unit="{request}",
            description="HTTP requests started in the last thirty seconds.",
        ),
        active_websockets=meter.create_gauge(
            "omnigent.server.websocket.connections.active",
            unit="{connection}",
            description="Accepted WebSocket connections currently open.",
        ),
    )


def _create_processing_otel_instruments(
    meter: MeterLike,
) -> _ProcessingOtelInstruments:
    """
    Create OpenTelemetry instruments for request processing durations.

    :param meter: Meter used to create instruments.
    :returns: Grouped processing-duration instruments.
    """
    return _ProcessingOtelInstruments(
        avg_ms=meter.create_gauge(
            "omnigent.server.http.request.processing.avg",
            unit="ms",
            description="Average HTTP request processing duration since process start.",
        ),
        max_ms=meter.create_gauge(
            "omnigent.server.http.request.processing.max",
            unit="ms",
            description="Maximum HTTP request processing duration since process start.",
        ),
        avg_1s_ms=meter.create_gauge(
            "omnigent.server.http.request.processing.avg_1s",
            unit="ms",
            description=("Average processing duration for requests completed in the last second."),
        ),
        avg_10s_ms=meter.create_gauge(
            "omnigent.server.http.request.processing.avg_10s",
            unit="ms",
            description=(
                "Average processing duration for requests completed in the last ten seconds."
            ),
        ),
        avg_30s_ms=meter.create_gauge(
            "omnigent.server.http.request.processing.avg_30s",
            unit="ms",
            description=(
                "Average processing duration for requests completed in the last thirty seconds."
            ),
        ),
    )


def _create_resource_otel_instruments(meter: MeterLike) -> _ResourceOtelInstruments:
    """
    Create OpenTelemetry instruments for process and system resources.

    :param meter: Meter used to create instruments.
    :returns: Grouped resource instruments.
    """
    return _ResourceOtelInstruments(
        cpu_percent=meter.create_gauge(
            "omnigent.server.process.cpu.percent",
            unit="%",
            description="Process CPU utilization since the previous server metrics snapshot.",
        ),
        load_1m=meter.create_gauge(
            "omnigent.server.system.load_average.1m",
            unit="{load}",
            description="One-minute system load average.",
        ),
        load_5m=meter.create_gauge(
            "omnigent.server.system.load_average.5m",
            unit="{load}",
            description="Five-minute system load average.",
        ),
        load_15m=meter.create_gauge(
            "omnigent.server.system.load_average.15m",
            unit="{load}",
            description="Fifteen-minute system load average.",
        ),
        rss_bytes=meter.create_gauge(
            "omnigent.server.process.memory.rss",
            unit="By",
            description="Resident memory used by the server process.",
        ),
    )


class ServerMetricsOtelPublisher:
    """
    Publish server performance metrics through OpenTelemetry instruments.

    Periodic snapshot values are exported from a single background
    publisher. That avoids an independent observable callback
    perturbing the CPU-delta state inside
    :class:`ServerPerformanceMetrics`.

    :param meter: Optional meter used to create instruments. ``None``
        uses ``opentelemetry.metrics.get_meter`` with the package's
        server performance meter name.
    """

    def __init__(self, meter: MeterLike | None = None) -> None:
        """
        Initialize OpenTelemetry instruments.

        :param meter: Optional meter used by tests to inject real
            recording stubs. ``None`` uses the global OpenTelemetry
            meter provider.
        """
        effective_meter = meter or otel_metrics.get_meter(_OTEL_METER_NAME)
        self._counter_state = _CounterState()
        self._request = _create_request_otel_instruments(effective_meter)
        self._processing = _create_processing_otel_instruments(effective_meter)
        self._resource = _create_resource_otel_instruments(effective_meter)

    def record_request_duration(
        self,
        *,
        duration_seconds: float,
        failed: bool,
        method: str,
        route: str,
        status_code: int | None,
    ) -> None:
        """
        Record one completed HTTP request duration.

        :param duration_seconds: Request processing duration in
            seconds.
        :param failed: Whether the request failed by exception or
            5xx status.
        :param method: HTTP request method, e.g. ``"GET"``.
        :param route: FastAPI route template, e.g.
            ``"/v1/sessions/{session_id}"``.
        :param status_code: HTTP response status code, e.g. ``200``.
            ``None`` when no response status was available.
        """
        attributes: dict[str, str | bool | int] = {
            "failed": failed,
            "http.request.method": method,
            "http.route": route,
        }
        if status_code is not None:
            attributes["http.response.status_code"] = status_code
        self._request.duration.record(
            duration_seconds,
            attributes=attributes,
        )

    def publish(self, snapshot: ServerMetricsSnapshot) -> None:
        """
        Publish one server metrics snapshot through OTEL instruments.

        :param snapshot: Snapshot produced by
            :meth:`ServerPerformanceMetrics.snapshot`.
        """
        self._publish_counter_deltas(snapshot)
        self._request.in_flight.set(snapshot.in_flight)
        self._request.last_1s.set(snapshot.requests_last_1s)
        self._request.last_10s.set(snapshot.requests_last_10s)
        self._request.last_30s.set(snapshot.requests_last_30s)
        self._request.active_websockets.set(snapshot.active_websockets)
        self._processing.avg_ms.set(snapshot.request_processing_avg_ms)
        self._processing.max_ms.set(snapshot.request_processing_max_ms)
        self._processing.avg_1s_ms.set(snapshot.request_processing_avg_1s_ms)
        self._processing.avg_10s_ms.set(snapshot.request_processing_avg_10s_ms)
        self._processing.avg_30s_ms.set(snapshot.request_processing_avg_30s_ms)
        self._resource.cpu_percent.set(snapshot.process_cpu_percent)
        if snapshot.load_average_1m is not None:
            self._resource.load_1m.set(snapshot.load_average_1m)
        if snapshot.load_average_5m is not None:
            self._resource.load_5m.set(snapshot.load_average_5m)
        if snapshot.load_average_15m is not None:
            self._resource.load_15m.set(snapshot.load_average_15m)
        self._resource.rss_bytes.set(snapshot.rss_bytes)

    def _publish_counter_deltas(self, snapshot: ServerMetricsSnapshot) -> None:
        """
        Publish cumulative snapshot counters as monotonic deltas.

        :param snapshot: Snapshot containing cumulative totals.
        """
        self._add_delta(
            self._request.started,
            snapshot.total_started - self._counter_state.total_started,
        )
        self._add_delta(
            self._request.completed,
            snapshot.total_completed - self._counter_state.total_completed,
        )
        self._add_delta(
            self._request.failed,
            snapshot.total_failed - self._counter_state.total_failed,
        )
        self._counter_state.total_started = snapshot.total_started
        self._counter_state.total_completed = snapshot.total_completed
        self._counter_state.total_failed = snapshot.total_failed

    def _add_delta(self, counter: CounterInstrument, amount: int) -> None:
        """
        Add a non-negative delta to an OpenTelemetry counter.

        :param counter: Counter instrument to update.
        :param amount: Delta since the prior published snapshot.
        """
        if amount > 0:
            counter.add(amount)


def set_request_duration_for_access_log(duration_seconds: float | None) -> None:
    """
    Store the current request duration for Uvicorn access formatting.

    :param duration_seconds: Request processing duration in seconds,
        or ``None`` to clear the value for a test or non-request
        context.
    """
    _REQUEST_DURATION_CONTEXT.set(duration_seconds)


def set_request_id_for_access_log(request_id: str | None) -> None:
    """
    Store the request correlation ID for Uvicorn access formatting.

    :param request_id: UUID hex identifying this request, or ``None``
        to clear.
    """
    _REQUEST_ID_CONTEXT.set(request_id)


def set_request_user_agent_for_access_log(user_agent: str | None) -> None:
    """
    Store the User-Agent header for Uvicorn access formatting.

    :param user_agent: Raw ``User-Agent`` header value, or ``None``
        to clear.
    """
    _REQUEST_USER_AGENT_CONTEXT.set(user_agent)


def set_request_session_id_for_access_log(session_id: str | None) -> None:
    """
    Store the session ID for Uvicorn access formatting.

    :param session_id: Session/conversation ID extracted from the
        request path, or ``None`` when the path does not target a
        session.
    """
    _REQUEST_SESSION_ID_CONTEXT.set(session_id)


async def publish_server_metrics_periodically(
    metrics: ServerPerformanceMetrics,
    *,
    otel_publisher: ServerMetricsOtelPublisher,
    interval_seconds: float = 10.0,
) -> None:
    """
    Publish server metrics snapshots to OpenTelemetry until cancelled.

    :param metrics: Metrics tracker to snapshot.
    :param otel_publisher: OpenTelemetry publisher that emits
        snapshot values as metrics.
    :param interval_seconds: Delay between OTEL snapshots in seconds,
        e.g. ``10.0``.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        otel_publisher.publish(metrics.snapshot())


def _load_average() -> SystemLoadAverage | None:
    """
    Return system load averages when the platform supports them.

    :returns: One-, five-, and fifteen-minute load averages, or
        ``None`` on platforms without ``os.getloadavg`` support.
    """
    try:
        one_minute, five_minutes, fifteen_minutes = os.getloadavg()
        return SystemLoadAverage(
            one_minute=one_minute,
            five_minutes=five_minutes,
            fifteen_minutes=fifteen_minutes,
        )
    except (AttributeError, OSError):
        return None


def _current_rss_bytes() -> int:
    """
    Return process resident memory in bytes.

    Linux exposes current RSS in ``/proc/self/status``. macOS falls
    back to ``resource.getrusage().ru_maxrss`` (maximum RSS, the best
    stdlib-only option there). Windows has no ``resource`` module, so it
    falls back to :mod:`psutil` (a core dependency), which reports true
    current RSS.

    :returns: Resident memory in bytes.
    """
    proc_status = "/proc/self/status"
    try:
        with open(proc_status, encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) * 1024
    except (FileNotFoundError, OSError, ValueError):
        pass

    if resource is not None:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        if sys.platform == "darwin":
            return int(usage.ru_maxrss)
        return int(usage.ru_maxrss) * 1024

    import psutil

    return int(psutil.Process().memory_info().rss)
