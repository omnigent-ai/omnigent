"""Tests for client-side WebSocket lifecycle metrics."""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field

import pytest
from opentelemetry.util.types import Attributes
from websockets.exceptions import ConnectionClosedError

from omnigent.runtime.websocket_metrics import (
    CONNECTIONS_METRIC_NAME,
    DISCONNECTIONS_METRIC_NAME,
    ClientWebSocketMetrics,
    classify_disconnect_reason,
    record_websocket_connected,
    record_websocket_disconnected,
    websocket_close_code,
    websocket_close_reason,
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


@dataclass(frozen=True)
class _Close:
    """Minimal close metadata carried by ``websockets`` exceptions."""

    code: int
    reason: str = ""


class _Closed(Exception):
    """Exception carrying received WebSocket close metadata."""

    def __init__(self, code: int, reason: str = "") -> None:
        """Create an exception with a received close frame."""
        super().__init__(f"closed {code}: {reason}")
        self.rcvd = _Close(code, reason)


@pytest.mark.parametrize(
    ("error", "kwargs", "expected"),
    [
        (None, {"local_shutdown": True}, "local_shutdown"),
        (_Closed(1006), {"resumed_from_suspend": True}, "suspend_resume"),
        (_Closed(4003, "server reassigned; reconnecting"), {}, "server_rehome"),
        (_Closed(1012, "service restart"), {}, "server_restart"),
        (_Closed(4003, "ping timeout"), {}, "ping_timeout"),
        (_Closed(4004, "unauthenticated"), {}, "authentication_error"),
        (_Closed(1002, "bad frame"), {}, "protocol_error"),
        (ConnectionResetError("connection reset"), {}, "transport_error"),
        (_Closed(1000, "normal closure"), {}, "peer_closed"),
        (RuntimeError("unexpected"), {}, "unknown"),
    ],
)
def test_classify_disconnect_reason_is_bounded(
    error: BaseException | None,
    kwargs: dict[str, bool],
    expected: str,
) -> None:
    """Raw failures map to a fixed reason vocabulary."""
    assert classify_disconnect_reason(error, **kwargs) == expected


def test_code_less_connection_closed_avoids_deprecated_properties() -> None:
    """A no-close-frame drop classifies without deprecated attribute access."""
    error = ConnectionClosedError(None, None)

    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert websocket_close_code(error) is None
        assert websocket_close_reason(error) is None
        assert classify_disconnect_reason(error) == "transport_error"


def test_records_host_and_runner_connection_types() -> None:
    """Connection counters distinguish initial connects from reconnects."""
    meter = _Meter()
    metrics = ClientWebSocketMetrics(meter)

    metrics.record_connected("host", reconnect=False)
    metrics.record_connected("runner", reconnect=True)

    assert meter.counters[CONNECTIONS_METRIC_NAME].records == [
        _Record(1, {"tunnel.kind": "host", "connection.type": "initial"}),
        _Record(1, {"tunnel.kind": "runner", "connection.type": "reconnect"}),
    ]


def test_records_normalized_disconnect_and_close_code() -> None:
    """Disconnect metrics contain bounded reason and close-code attributes."""
    meter = _Meter()
    metrics = ClientWebSocketMetrics(meter)

    metrics.record_disconnected(
        "host",
        _Closed(4003, "server reassigned; reconnecting"),
    )

    assert meter.counters[DISCONNECTIONS_METRIC_NAME].records == [
        _Record(
            1,
            {
                "tunnel.kind": "host",
                "disconnect.reason": "server_rehome",
                "websocket.close.code": 4003,
            },
        )
    ]


def test_public_recorders_respect_telemetry_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Public helpers remain no-ops unless telemetry is explicitly enabled."""
    calls: list[ClientWebSocketMetrics] = []
    monkeypatch.delenv("OMNIGENT_TELEMETRY_ENABLED", raising=False)
    monkeypatch.setattr(
        "omnigent.runtime.websocket_metrics._default_metrics",
        lambda: calls.append(ClientWebSocketMetrics(_Meter())) or calls[-1],
    )

    record_websocket_connected("runner", reconnect=False)
    record_websocket_disconnected("runner", None)

    assert calls == []
