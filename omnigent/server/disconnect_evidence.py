"""Bounded tunnel-drop facts kept for delayed failure logging; no free text or identity."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from omnigent.runner.transports.ws_tunnel.frames import RUNNER_IDLE_SHUTDOWN_CLOSE_REASON
from omnigent.runner.transports.ws_tunnel.registry import RunnerSession

CloseCategory = Literal["idle_shutdown", "server_closed", "abnormal", "runner_closed", "none"]
# The code a socket reports when it dropped without any close frame.
_ABNORMAL_CLOSE_CODE = 1006


@dataclass(frozen=True)
class DisconnectEvidence:
    """What the server knew about one runner drop.

    :param disconnected_at: Unix epoch seconds when the tunnel's socket loop ended.
    :param close_code: The runner's close code, else the one this server
        requested, e.g. ``1006``; ``None`` if neither side sent one.
    :param close_reason_category: Who ended the tunnel and how; see
        :func:`DisconnectEvidence.from_session` for the precedence.
    :param connection_age_s: Seconds the tunnel stayed up before the drop.
    """

    disconnected_at: float
    close_code: int | None
    close_reason_category: CloseCategory
    connection_age_s: float

    @classmethod
    def from_session(cls, session: RunnerSession) -> DisconnectEvidence | None:
        """Read the drop from the tunnel's end snapshot, or ``None`` if it has none.

        A runner idle shutdown wins, then a close this server requested first,
        then a drop with no close frame (``1006``), then any other runner close.
        """
        end = session.ended
        if end is None:
            return None
        category: CloseCategory
        if end.peer_reason == RUNNER_IDLE_SHUTDOWN_CLOSE_REASON:
            category = "idle_shutdown"
        elif end.server_code is not None:
            category = "server_closed"
        elif end.peer_code == _ABNORMAL_CLOSE_CODE:
            category = "abnormal"
        elif end.peer_code is not None:
            category = "runner_closed"
        else:
            category = "none"
        return cls(
            disconnected_at=end.at,
            close_code=end.peer_code if end.peer_code is not None else end.server_code,
            close_reason_category=category,
            connection_age_s=round(max(0.0, end.at - session.connected_at), 3),
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
