"""Drop-time facts about a runner tunnel, kept until its held turn is failed.

A turn can wait on an offline host before it fails, so the failure row is
written long after the drop. These fields let that row name the original drop
without a time-window lookup. Only bounded values are kept: no close reason
text, host, or user identity.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass

from omnigent.runner.transports.ws_tunnel.frames import RUNNER_IDLE_SHUTDOWN_CLOSE_REASON
from omnigent.runner.transports.ws_tunnel.registry import RunnerSession


@dataclass(frozen=True)
class DisconnectEvidence:
    """What the server knew about one runner drop.

    :param disconnected_at: Unix epoch seconds when the drop was observed.
    :param close_code: WebSocket close code, e.g. ``1006``; ``None`` if unknown.
    :param close_reason_category: ``"idle_shutdown"``, ``"runner_closed"``,
        ``"server_closed"``, or ``"none"``.
    :param connection_age_s: Seconds the tunnel stayed up before the drop.
    """

    disconnected_at: float
    close_code: int | None
    close_reason_category: str
    connection_age_s: float

    @classmethod
    def from_session(cls, session: RunnerSession, *, now: float) -> DisconnectEvidence:
        """Read the drop facts from the tunnel that just closed."""
        if session.peer_close_reason == RUNNER_IDLE_SHUTDOWN_CLOSE_REASON:
            category = "idle_shutdown"
        elif session.peer_close_reason:
            category = "runner_closed"
        elif session.close_reason:
            category = "server_closed"
        else:
            category = "none"
        code = session.peer_close_code
        if code is None:
            code = session.close_code
        return cls(
            disconnected_at=now,
            close_code=code,
            close_reason_category=category,
            connection_age_s=round(max(0.0, now - session.connected_at), 3),
        )

    def log_attributes(self, *, hold_s: float | None, hold_outcome: str | None) -> dict[str, str]:
        """Flatten for a structured log row; string values only."""
        disconnected = dt.datetime.fromtimestamp(self.disconnected_at, dt.UTC)
        return {
            "drop_disconnected_at": disconnected.isoformat(timespec="milliseconds"),
            "drop_close_code": "unknown" if self.close_code is None else str(self.close_code),
            "drop_close_reason_category": self.close_reason_category,
            "drop_connection_age_s": str(self.connection_age_s),
            "hold_s": "none" if hold_s is None else str(round(hold_s, 3)),
            "hold_outcome": hold_outcome or "none",
        }


def drop_attributes(
    evidence: DisconnectEvidence | None,
    *,
    hold_s: float | None,
    hold_outcome: str | None,
) -> Mapping[str, str]:
    """Return the log attributes, or nothing when the drop was not captured."""
    if evidence is None:
        return {}
    return evidence.log_attributes(hold_s=hold_s, hold_outcome=hold_outcome)
