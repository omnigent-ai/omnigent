"""Compatibility imports for client-side WebSocket lifecycle metrics."""

from omnigent.websocket_metrics import (
    CONNECT_BOOTSTRAP_DURATION_METRIC_NAME,
    CONNECTIONS_METRIC_NAME,
    DISCONNECTIONS_METRIC_NAME,
    ClientWebSocketMetrics,
    DisconnectReason,
    TunnelKind,
    classify_disconnect_reason,
    record_websocket_connect_bootstrap,
    record_websocket_connected,
    record_websocket_disconnected,
    telemetry_enabled,
    websocket_close_code,
    websocket_close_reason,
)

__all__ = [
    "CONNECTIONS_METRIC_NAME",
    "CONNECT_BOOTSTRAP_DURATION_METRIC_NAME",
    "DISCONNECTIONS_METRIC_NAME",
    "ClientWebSocketMetrics",
    "DisconnectReason",
    "TunnelKind",
    "classify_disconnect_reason",
    "record_websocket_connect_bootstrap",
    "record_websocket_connected",
    "record_websocket_disconnected",
    "telemetry_enabled",
    "websocket_close_code",
    "websocket_close_reason",
]
