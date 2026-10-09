"""Connection-local tunnel timings; none of these observations drive liveness.

Heartbeat fields describe application PingFrame/PongFrame traffic, not WebSocket
control frames. All durations use the local monotonic clock. History is bounded;
queue timestamps travel with frames already retained by the outbound queue.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TypedDict, cast

_logger = logging.getLogger(__name__)

_SAMPLE_INTERVAL_S = 5.0
_SLOW_OPERATION_S = 1.0
_REPORT_INTERVAL_S = 60.0
_MAX_TRACKED_SENDS = 64
_MAX_TRACKED_PINGS = 8


class TunnelKeepaliveSettings(TypedDict, total=False):
    """Only budgets observable on this end of the connection."""

    tunnel_side: str
    app_ping_interval_s: float
    app_silence_timeout_s: float
    protocol_keepalive_source: str
    protocol_ping_interval_s: float | None
    protocol_ping_timeout_s: float | None


class TunnelDiagnosticAttrs(TunnelKeepaliveSettings, total=False):
    """Scalar log attributes, with no lifecycle identity or payload fields."""

    loop_sample_interval_s: float
    sampler_failed: bool
    diagnostics_age_s: float
    sends_in_flight: int
    oldest_tracked_send_age_s: float | None
    send_samples_dropped: int
    send_errors: int
    send_cancellations: int
    last_send_outcome: str | None
    outbound_queue_depth: int | None
    outbound_queue_high_water: int | None
    app_pings_queued: int
    app_ping_samples_dropped: int
    loop_lag_s: float | None
    loop_lag_max_s: float | None
    loop_lag_max_age_s: float | None
    send_duration_s: float | None
    send_duration_max_s: float | None
    send_duration_max_age_s: float | None
    queue_wait_s: float | None
    queue_wait_max_s: float | None
    queue_wait_max_age_s: float | None
    enqueue_delay_s: float | None
    enqueue_delay_max_s: float | None
    enqueue_delay_max_age_s: float | None
    app_ping_rtt_s: float | None
    app_ping_rtt_max_s: float | None
    app_ping_rtt_max_age_s: float | None
    last_received_frame_age_s: float | None
    last_sent_frame_age_s: float | None
    last_app_ping_queued_age_s: float | None
    last_app_ping_sent_age_s: float | None
    last_app_pong_received_age_s: float | None
    last_app_ping_received_age_s: float | None
    last_app_pong_sent_age_s: float | None


@dataclass(frozen=True, slots=True)
class OutboundFrame:
    """An unchanged wire payload timestamped with its diagnostics' monotonic clock."""

    data: str
    queued_at: float
    app_ping_ts: int | None = None


@dataclass
class _Timing:
    last_s: float | None = None
    max_s: float | None = None
    max_at: float | None = None

    def observe(self, seconds: float, now: float) -> None:
        self.last_s = max(0.0, seconds)
        if self.max_s is None or self.last_s > self.max_s:
            self.max_s = self.last_s
            self.max_at = now

    def attributes(self, name: str, now: float) -> dict[str, float | None]:
        return {
            f"{name}_s": _rounded(self.last_s),
            f"{name}_max_s": _rounded(self.max_s),
            f"{name}_max_age_s": _age(now, self.max_at),
        }


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


def _age(now: float, then: float | None) -> float | None:
    return None if then is None else round(max(0.0, now - then), 3)


class TunnelDiagnostics:
    """Observe one socket on its owner loop, retaining no payloads or task refs.

    Snapshots are frozen before teardown so cancelled helpers cannot erase an
    outstanding send. Maxima cover this connection's lifetime and carry an age
    to distinguish an old delay from one near the disconnect.

    :param clock: Local monotonic clock, injectable for deterministic timing tests.
        Queue timestamps must use this same clock.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self.settings: TunnelKeepaliveSettings = {}
        self._loop_lag = _Timing()
        self._send_duration = _Timing()
        self._queue_wait = _Timing()
        self._enqueue_delay = _Timing()
        self._ping_rtt = _Timing()
        self._observed_at: dict[str, float] = {}
        self._active_sends: dict[object, float] = {}
        self._sends_in_flight = 0
        self._send_samples_dropped = 0
        self._send_errors = 0
        self._send_cancellations = 0
        self._last_send_outcome: str | None = None
        self._queue_depth: int | None = None
        self._queue_high_water: int | None = None
        self._queued_pings = 0
        self._pings: dict[int, float] = {}
        self._ping_samples_dropped = 0
        self._loop_due_at: float | None = None
        self._sampler_failed = False
        self._report: Callable[[], None] | None = None
        self._next_report_at = 0.0
        self._frozen: TunnelDiagnosticAttrs | None = None
        self._frozen_at = 0.0

    def timestamp(self) -> float:
        """Read this connection's monotonic clock for queue timing."""
        return self._clock()

    @contextlib.asynccontextmanager
    async def monitoring(
        self, report: Callable[[], None], *, connection_id: str | None = None
    ) -> AsyncIterator[None]:
        """Sample scheduling delays and report slow operations at most once/minute."""
        self._report = report
        self._loop_due_at = self._clock() + _SAMPLE_INTERVAL_S
        task = asyncio.create_task(self._monitor(connection_id), name="tunnel-diagnostics")
        try:
            yield
        finally:
            try:
                self.freeze()
            finally:
                task.cancel()
                self._loop_due_at = None
                self._report = None
                await asyncio.gather(task, return_exceptions=True)

    async def _monitor(self, connection_id: str | None) -> None:
        try:
            while (due_at := self._loop_due_at) is not None:
                await asyncio.sleep(max(0.0, due_at - self._clock()))
                now = self._clock()
                lag = max(0.0, now - due_at)
                self._loop_lag.observe(lag, now)
                self._loop_due_at = now + _SAMPLE_INTERVAL_S
                oldest_send = min(self._active_sends.values(), default=now)
                if max(lag, now - oldest_send) >= _SLOW_OPERATION_S:
                    self._maybe_report(now)
        except Exception:
            # A dead sampler must not look like an ever-growing scheduling delay.
            self._loop_due_at = None
            self._sampler_failed = True
            with contextlib.suppress(Exception):
                _logger.exception(
                    "Tunnel diagnostics monitor failed",
                    extra={
                        "attributes": {
                            "connection_id": connection_id,
                            "tunnel_side": self.settings.get("tunnel_side"),
                        }
                    },
                )

    def _maybe_report(self, now: float) -> None:
        if self._report is not None and self._frozen is None and now >= self._next_report_at:
            self._next_report_at = now + _REPORT_INTERVAL_S
            # Diagnostic sinks must not interfere with socket I/O or cleanup.
            with contextlib.suppress(Exception):
                self._report()

    def frame_received(self) -> None:
        self._observed_at["last_received_frame"] = self._clock()

    def app_ping_received(self) -> None:
        self._observed_at["last_app_ping_received"] = self._clock()

    def app_pong_sent(self) -> None:
        self._observed_at["last_app_pong_sent"] = self._clock()

    def app_pong_received(self, ts: int) -> None:
        now = self._clock()
        self._observed_at["last_app_pong_received"] = now
        sent_at = self._pings.pop(ts, None)
        if sent_at is not None:
            self._ping_rtt.observe(now - sent_at, now)

    def enqueued(self, frame: OutboundFrame, depth: int, requested_at: float) -> None:
        """Record queue entry; both input timestamps must use this object's clock."""
        self._queue_depth = depth
        self._queue_high_water = max(self._queue_high_water or 0, depth)
        self._enqueue_delay.observe(frame.queued_at - requested_at, frame.queued_at)
        if frame.app_ping_ts is not None:
            self._queued_pings += 1
            self._observed_at["last_app_ping_queued"] = frame.queued_at

    def dequeued(self, frame: OutboundFrame) -> None:
        now = self._clock()
        # Count data frames only; retirement also enqueues a None sentinel.
        if self._queue_depth is not None:
            self._queue_depth = max(0, self._queue_depth - 1)
        self._queue_wait.observe(now - frame.queued_at, now)
        if frame.app_ping_ts is not None:
            self._queued_pings = max(0, self._queued_pings - 1)
        if now - frame.queued_at >= _SLOW_OPERATION_S:
            self._maybe_report(now)

    async def send(
        self,
        send_text: Callable[[str], Awaitable[None]],
        data: str,
        *,
        app_ping_ts: int | None = None,
    ) -> None:
        """Observe send completion, exceptions and cancellation without altering them."""
        started = self._clock()
        token = object()
        self._sends_in_flight += 1
        if len(self._active_sends) < _MAX_TRACKED_SENDS:
            self._active_sends[token] = started
        else:
            self._send_samples_dropped += 1
        if app_ping_ts is not None:
            self._pings[app_ping_ts] = started
            if len(self._pings) > _MAX_TRACKED_PINGS:
                self._pings.pop(next(iter(self._pings)))
                self._ping_samples_dropped += 1
        outcome = "completed"
        try:
            await send_text(data)
        except asyncio.CancelledError:
            outcome = "cancelled"
            self._send_cancellations += 1
            raise
        except Exception:
            outcome = "error"
            self._send_errors += 1
            raise
        else:
            now = self._clock()
            self._observed_at["last_sent_frame"] = now
            if app_ping_ts is not None:
                self._observed_at["last_app_ping_sent"] = now
        finally:
            now = self._clock()
            self._send_duration.observe(now - started, now)
            self._last_send_outcome = outcome
            self._sends_in_flight -= 1
            self._active_sends.pop(token, None)
            if now - started >= _SLOW_OPERATION_S:
                self._maybe_report(now)

    def snapshot(self) -> TunnelDiagnosticAttrs:
        """Return scalar timings; missing observations stay null, never zero ages."""
        if self._frozen is not None:
            return {
                **self._frozen,
                "diagnostics_age_s": round(max(0.0, self._clock() - self._frozen_at), 3),
            }
        now = self._clock()
        loop_lag = self._loop_lag
        # A close callback may run before the overdue monitor gets its turn.
        if self._loop_due_at is not None and now > self._loop_due_at:
            loop_lag = replace(loop_lag)
            loop_lag.observe(now - self._loop_due_at, now)
        attrs: dict[str, object] = {
            **self.settings,
            "diagnostics_age_s": 0.0,
            "loop_sample_interval_s": _SAMPLE_INTERVAL_S,
            "sampler_failed": self._sampler_failed,
            "sends_in_flight": self._sends_in_flight,
            "oldest_tracked_send_age_s": _age(now, min(self._active_sends.values(), default=None)),
            "send_samples_dropped": self._send_samples_dropped,
            "send_errors": self._send_errors,
            "send_cancellations": self._send_cancellations,
            "last_send_outcome": self._last_send_outcome,
            "outbound_queue_depth": self._queue_depth,
            "outbound_queue_high_water": self._queue_high_water,
            "app_pings_queued": self._queued_pings,
            "app_ping_samples_dropped": self._ping_samples_dropped,
        }
        for name, timing in (
            ("loop_lag", loop_lag),
            ("send_duration", self._send_duration),
            ("queue_wait", self._queue_wait),
            ("enqueue_delay", self._enqueue_delay),
            ("app_ping_rtt", self._ping_rtt),
        ):
            attrs.update(timing.attributes(name, now))
        for name in (
            "last_received_frame",
            "last_sent_frame",
            "last_app_ping_queued",
            "last_app_ping_sent",
            "last_app_pong_received",
            "last_app_ping_received",
            "last_app_pong_sent",
        ):
            attrs[f"{name}_age_s"] = _age(now, self._observed_at.get(name))
        return cast(TunnelDiagnosticAttrs, attrs)

    def freeze(self) -> None:
        """Retain the evidence at disconnect, before shutdown cancels blocked sends."""
        if self._frozen is None:
            self._frozen = self.snapshot()
            self._frozen_at = self._clock()
