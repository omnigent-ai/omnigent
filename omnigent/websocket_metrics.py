"""OpenTelemetry metrics for client-side WebSocket tunnel lifecycle.

The host imports this before its first tunnel. Keep it outside
``omnigent.runtime`` so importing the metric helpers does not initialize the
server runtime and database/model graph.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Literal, Protocol

from opentelemetry import metrics as otel_metrics
from opentelemetry.util.types import Attributes
from websockets.exceptions import ConnectionClosed

_logger = logging.getLogger(__name__)

_OTEL_METER_NAME = "omnigent.client.websocket"

CONNECTIONS_METRIC_NAME = "omnigent.client.websocket.connections"
DISCONNECTIONS_METRIC_NAME = "omnigent.client.websocket.disconnections"
CONNECT_BOOTSTRAP_DURATION_METRIC_NAME = "omnigent.client.websocket.connect_bootstrap.duration"

TunnelKind = Literal["host", "runner"]
DisconnectReason = Literal[
    "authentication_error",
    "local_shutdown",
    "peer_closed",
    "ping_timeout",
    "protocol_error",
    "server_rehome",
    "server_restart",
    "suspend_resume",
    "transport_error",
    "unknown",
]


class _CounterLike(Protocol):
    """Subset of the OpenTelemetry counter API used by this module."""

    def add(self, amount: int | float, attributes: Attributes = None) -> None:
        """Add a value with optional metric attributes."""


class _HistogramLike(Protocol):
    """Subset of the OpenTelemetry histogram API used by this module."""

    def record(self, amount: int | float, attributes: Attributes = None) -> None:
        """Record one observation with optional metric attributes."""


class _MeterLike(Protocol):
    """Subset of the OpenTelemetry meter API used by this module."""

    def create_counter(
        self,
        name: str,
        unit: str = "",
        description: str = "",
    ) -> _CounterLike:
        """Create a monotonic counter."""

    def create_histogram(
        self,
        name: str,
        unit: str = "",
        description: str = "",
    ) -> _HistogramLike:
        """Create a histogram."""


def telemetry_enabled() -> bool:
    """Return whether this process opted into Omnigent telemetry."""
    return os.environ.get("OMNIGENT_TELEMETRY_ENABLED", "").strip().lower() in (
        "true",
        "1",
        "yes",
    )


def websocket_close_code(error: BaseException | None) -> int | None:
    """Return a WebSocket close code carried by an exception."""
    if error is None:
        return None
    for attr in ("rcvd", "sent"):
        close = getattr(error, attr, None)
        code = getattr(close, "code", None)
        if isinstance(code, int):
            return code
    if isinstance(error, ConnectionClosed):
        return None
    direct = getattr(error, "code", None)
    if isinstance(direct, int):
        return direct
    return None


def websocket_close_reason(error: BaseException | None) -> str | None:
    """Return a WebSocket close reason carried by an exception."""
    if error is None:
        return None
    for attr in ("rcvd", "sent"):
        close = getattr(error, attr, None)
        reason = getattr(close, "reason", None)
        if isinstance(reason, str) and reason:
            return reason
    if isinstance(error, ConnectionClosed):
        return None
    direct = getattr(error, "reason", None)
    if isinstance(direct, str) and direct:
        return direct
    return None


def classify_disconnect_reason(
    error: BaseException | None,
    *,
    local_shutdown: bool = False,
    resumed_from_suspend: bool = False,
) -> DisconnectReason:
    """Map tunnel termination details to a bounded reason."""
    if local_shutdown:
        return "local_shutdown"
    if resumed_from_suspend:
        return "suspend_resume"

    code = websocket_close_code(error)
    close_reason = websocket_close_reason(error) or ""
    error_text = str(error) if error is not None else ""
    detail = f"{close_reason} {error_text}".lower()

    if any(token in detail for token in ("reassigned", "re-home", "rehome")):
        return "server_rehome"
    if code == 1012 or "service restart" in detail:
        return "server_restart"
    if "ping timeout" in detail or "keepalive ping" in detail:
        return "ping_timeout"
    if code == 4004 or "unauthenticated" in detail or "authentication" in detail:
        return "authentication_error"
    if code in {1002, 1003, 1007, 1008, 4001, 4002, 4500} or "protocol" in detail:
        return "protocol_error"
    if (
        code == 1006
        or isinstance(error, ConnectionError | OSError)
        or any(
            token in detail
            for token in (
                "connection reset",
                "connection refused",
                "no close frame",
                "timed out",
                "transport",
            )
        )
    ):
        return "transport_error"
    if error is None or code is not None:
        return "peer_closed"
    return "unknown"


class ClientWebSocketMetrics:
    """Record bounded host and runner tunnel lifecycle metrics."""

    def __init__(self, meter: _MeterLike | None = None) -> None:
        """Create the connection and disconnection counters."""
        effective_meter = meter or otel_metrics.get_meter(_OTEL_METER_NAME)
        self._connections = effective_meter.create_counter(
            CONNECTIONS_METRIC_NAME,
            unit="{connection}",
            description="Accepted client WebSocket tunnel connections.",
        )
        self._disconnections = effective_meter.create_counter(
            DISCONNECTIONS_METRIC_NAME,
            unit="{connection}",
            description="Ended client WebSocket tunnel connections.",
        )
        self._connect_bootstrap_duration = effective_meter.create_histogram(
            CONNECT_BOOTSTRAP_DURATION_METRIC_NAME,
            unit="ms",
            description="Cold host bootstrap phase duration through accepted WebSocket upgrade.",
        )

    def record_connect_bootstrap(self, phases_ms: dict[str, float]) -> None:
        """Record bounded phase durations for one initial host connection."""
        for phase, duration_ms in phases_ms.items():
            self._connect_bootstrap_duration.record(
                duration_ms,
                attributes={"tunnel.kind": "host", "bootstrap.phase": phase},
            )

    def record_connected(self, kind: TunnelKind, *, reconnect: bool) -> None:
        """Record one accepted WebSocket upgrade."""
        self._connections.add(
            1,
            attributes={
                "tunnel.kind": kind,
                "connection.type": "reconnect" if reconnect else "initial",
            },
        )

    def record_disconnected(
        self,
        kind: TunnelKind,
        error: BaseException | None,
        *,
        local_shutdown: bool = False,
        resumed_from_suspend: bool = False,
    ) -> None:
        """Record one ended established WebSocket connection."""
        attributes: dict[str, str | int] = {
            "tunnel.kind": kind,
            "disconnect.reason": classify_disconnect_reason(
                error,
                local_shutdown=local_shutdown,
                resumed_from_suspend=resumed_from_suspend,
            ),
        }
        close_code = websocket_close_code(error)
        if close_code is not None:
            attributes["websocket.close.code"] = close_code
        self._disconnections.add(1, attributes=attributes)


@lru_cache(maxsize=1)
def _default_metrics() -> ClientWebSocketMetrics:
    """Return the process-wide WebSocket metric instruments."""
    return ClientWebSocketMetrics()


def record_websocket_connected(kind: TunnelKind, *, reconnect: bool) -> None:
    """Best-effort record of an accepted client WebSocket upgrade."""
    if not telemetry_enabled():
        return
    try:
        _default_metrics().record_connected(kind, reconnect=reconnect)
    except Exception:  # noqa: BLE001 - telemetry must never disrupt the tunnel
        _logger.debug("failed to record WebSocket connection metric", exc_info=True)


def record_websocket_connect_bootstrap(phases_ms: dict[str, float]) -> None:
    """Best-effort record of one cold host bootstrap decomposition."""
    if not telemetry_enabled():
        return
    try:
        _default_metrics().record_connect_bootstrap(phases_ms)
    except Exception:  # noqa: BLE001 - telemetry must never disrupt the tunnel
        _logger.debug("failed to record WebSocket bootstrap metric", exc_info=True)


def record_websocket_disconnected(
    kind: TunnelKind,
    error: BaseException | None,
    *,
    local_shutdown: bool = False,
    resumed_from_suspend: bool = False,
) -> None:
    """Best-effort record of an ended established WebSocket connection."""
    if not telemetry_enabled():
        return
    try:
        _default_metrics().record_disconnected(
            kind,
            error,
            local_shutdown=local_shutdown,
            resumed_from_suspend=resumed_from_suspend,
        )
    except Exception:  # noqa: BLE001 - telemetry must never disrupt the tunnel
        _logger.debug("failed to record WebSocket disconnection metric", exc_info=True)
