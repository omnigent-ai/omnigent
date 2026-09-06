"""OpenTelemetry metrics for client-side WebSocket tunnel lifecycle."""

from __future__ import annotations

import logging
from functools import lru_cache
from typing import Literal, Protocol

from opentelemetry import metrics as otel_metrics
from opentelemetry.util.types import Attributes
from websockets.exceptions import ConnectionClosed

from omnigent.runtime.telemetry import telemetry_enabled

_logger = logging.getLogger(__name__)

_OTEL_METER_NAME = "omnigent.client.websocket"

CONNECTIONS_METRIC_NAME = "omnigent.client.websocket.connections"
DISCONNECTIONS_METRIC_NAME = "omnigent.client.websocket.disconnections"

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


class _MeterLike(Protocol):
    """Subset of the OpenTelemetry meter API used by this module."""

    def create_counter(
        self,
        name: str,
        unit: str = "",
        description: str = "",
    ) -> _CounterLike:
        """Create a monotonic counter."""


def websocket_close_code(error: BaseException | None) -> int | None:
    """Return a WebSocket close code carried by an exception.

    ``websockets`` has exposed close metadata both directly and through
    ``rcvd`` / ``sent`` objects across supported versions.

    :param error: Tunnel exception, or ``None`` for a clean return.
    :returns: Close code such as ``1012`` or ``4003`` when available.
    """
    if error is None:
        return None
    for attr in ("rcvd", "sent"):
        close = getattr(error, attr, None)
        code = getattr(close, "code", None)
        if isinstance(code, int):
            return code
    if isinstance(error, ConnectionClosed):
        # ``ConnectionClosed.code`` is deprecated (websockets >=13.1) and
        # warns on every abnormal reconnect; ``rcvd``/``sent`` already cover
        # any real close frame, so a code-less close stays uncoded here.
        return None
    direct = getattr(error, "code", None)
    if isinstance(direct, int):
        return direct
    return None


def websocket_close_reason(error: BaseException | None) -> str | None:
    """Return a WebSocket close reason carried by an exception.

    :param error: Tunnel exception, or ``None`` for a clean return.
    :returns: Peer-provided close reason when available.
    """
    if error is None:
        return None
    for attr in ("rcvd", "sent"):
        close = getattr(error, attr, None)
        reason = getattr(close, "reason", None)
        if isinstance(reason, str) and reason:
            return reason
    if isinstance(error, ConnectionClosed):
        # ``ConnectionClosed.reason`` is deprecated (websockets >=13.1); see
        # websocket_close_code for why the direct fallback is skipped.
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
    """Map tunnel termination details to a bounded reason.

    Raw exception and peer reason text are used only for classification; they
    are never attached to the metric.

    :param error: Exception that ended the connection, or ``None`` for a clean
        peer close.
    :param local_shutdown: Whether the local process requested shutdown.
    :param resumed_from_suspend: Whether a suspend watcher dropped the stale
        socket to reconnect.
    :returns: A stable low-cardinality disconnect reason.
    """
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
        # Any unclassified close frame is an application-level close by the
        # peer; "unknown" is reserved for code-less unexpected exceptions.
        return "peer_closed"
    return "unknown"


class ClientWebSocketMetrics:
    """Record bounded host and runner tunnel lifecycle metrics.

    :param meter: Optional meter for tests. The global OpenTelemetry meter is
        used in production.
    """

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

    def record_connected(self, kind: TunnelKind, *, reconnect: bool) -> None:
        """Record one accepted WebSocket upgrade.

        :param kind: Tunnel client component.
        :param reconnect: Whether this follows an earlier accepted connection.
        """
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
        """Record one ended established WebSocket connection.

        :param kind: Tunnel client component.
        :param error: Exception that ended the connection, if any.
        :param local_shutdown: Whether the local process requested shutdown.
        :param resumed_from_suspend: Whether the stale socket was dropped after
            system resume.
        """
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
    except Exception:  # Telemetry must never disrupt the tunnel.
        _logger.debug("failed to record WebSocket connection metric", exc_info=True)


def record_websocket_disconnected(
    kind: TunnelKind,
    error: BaseException | None,
    *,
    local_shutdown: bool = False,
    resumed_from_suspend: bool = False,
) -> None:
    """Best-effort record of an ended client WebSocket connection."""
    if not telemetry_enabled():
        return
    try:
        _default_metrics().record_disconnected(
            kind,
            error,
            local_shutdown=local_shutdown,
            resumed_from_suspend=resumed_from_suspend,
        )
    except Exception:  # Telemetry must never disrupt the tunnel.
        _logger.debug("failed to record WebSocket disconnection metric", exc_info=True)
