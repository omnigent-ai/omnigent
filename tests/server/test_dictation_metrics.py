"""Tests for low-cardinality dictation OpenTelemetry instruments."""

from __future__ import annotations

from typing import Any

from omnigent.server import dictation_metrics


class _Instrument:
    def __init__(self) -> None:
        self.values: list[tuple[float, object]] = []

    def add(self, value: float, attributes: object = None) -> None:
        self.values.append((value, attributes))

    def record(self, value: float, attributes: object = None) -> None:
        self.values.append((value, attributes))


class _Meter:
    def __init__(self) -> None:
        self.instruments: dict[str, _Instrument] = {}

    def _create(self, name: str, **_: Any) -> _Instrument:
        instrument = _Instrument()
        self.instruments[name] = instrument
        return instrument

    create_counter = _create
    create_up_down_counter = _create
    create_histogram = _create


def test_dictation_metrics_use_bounded_outcome_attributes(monkeypatch: Any) -> None:
    meter = _Meter()
    monkeypatch.setattr(dictation_metrics.otel_metrics, "get_meter", lambda _: meter)
    recorder = dictation_metrics.DictationMetrics()

    recorder.started()
    recorder.audio_bytes(3200)
    recorder.completed("stopped")
    recorder.rejected("capacity")
    recorder.warmup(1.5, "ready")
    recorder.remote_connection("connected")
    recorder.fallback()

    assert meter.instruments["omnigent.server.dictation.takes.active"].values == [
        (1, None),
        (-1, None),
    ]
    assert meter.instruments["omnigent.server.dictation.takes.completed"].values == [
        (1, {"outcome": "stopped"})
    ]
    assert meter.instruments["omnigent.server.dictation.takes.rejected"].values == [
        (1, {"reason": "capacity"})
    ]
    assert meter.instruments["omnigent.server.dictation.audio.bytes"].values == [(3200, None)]
    assert meter.instruments["omnigent.server.dictation.warmup.duration"].values == [
        (1.5, {"outcome": "ready"})
    ]
