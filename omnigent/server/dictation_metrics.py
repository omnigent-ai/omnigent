"""Low-cardinality OpenTelemetry metrics for server-side dictation."""

from __future__ import annotations

from opentelemetry import metrics as otel_metrics


class DictationMetrics:
    """Record dictation lifecycle, resource, and relay metrics."""

    def __init__(self) -> None:
        meter = otel_metrics.get_meter("omnigent.server.dictation")
        self._started = meter.create_counter(
            "omnigent.server.dictation.takes.started",
            unit="{take}",
        )
        self._completed = meter.create_counter(
            "omnigent.server.dictation.takes.completed",
            unit="{take}",
        )
        self._rejected = meter.create_counter(
            "omnigent.server.dictation.takes.rejected",
            unit="{take}",
        )
        self._active = meter.create_up_down_counter(
            "omnigent.server.dictation.takes.active",
            unit="{take}",
        )
        self._audio_bytes = meter.create_counter(
            "omnigent.server.dictation.audio.bytes",
            unit="By",
        )
        self._warmup = meter.create_histogram(
            "omnigent.server.dictation.warmup.duration",
            unit="s",
        )
        self._remote_connections = meter.create_counter(
            "omnigent.server.dictation.remote.connections",
            unit="{connection}",
        )
        self._fallbacks = meter.create_counter(
            "omnigent.server.dictation.fallbacks",
            unit="{fallback}",
        )

    def started(self) -> None:
        self._started.add(1)
        self._active.add(1)

    def completed(self, outcome: str) -> None:
        self._completed.add(1, {"outcome": outcome})
        self._active.add(-1)

    def rejected(self, reason: str) -> None:
        self._rejected.add(1, {"reason": reason})

    def audio_bytes(self, count: int) -> None:
        self._audio_bytes.add(count)

    def warmup(self, duration_seconds: float, outcome: str) -> None:
        self._warmup.record(duration_seconds, {"outcome": outcome})

    def remote_connection(self, outcome: str) -> None:
        self._remote_connections.add(1, {"outcome": outcome})

    def fallback(self) -> None:
        self._fallbacks.add(1, {"reason": "remote_unavailable"})


metrics = DictationMetrics()
