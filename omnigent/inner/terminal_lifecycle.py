"""Small, content-free diagnostic history for one terminal launch."""

from __future__ import annotations

import json
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field

TERMINAL_INSTANCE_ID_ENV = "OMNIGENT_TERMINAL_INSTANCE_ID"
TERMINAL_LAUNCH_ID_ENV = "OMNIGENT_TERMINAL_LAUNCH_ID"
TERMINAL_LAUNCH_SESSION_ID_ENV = "OMNIGENT_TERMINAL_LAUNCH_SESSION_ID"


def lifecycle_log_attributes(context: dict[str, object]) -> dict[str, str]:
    """Encode bounded structures explicitly for the log sink's string-valued map."""
    return {
        key: json.dumps(value, separators=(",", ":"))
        if isinstance(value, (dict, list, bool))
        else str(value)
        for key, value in context.items()
        if value is not None
    }


@dataclass
class TerminalLifecycleTrace:
    """Diagnostics only: none of these observations determine terminal behavior."""

    session_id: str | None = None
    launch_session_id: str | None = None
    launch_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    launched_at: float | None = None
    exit_observed_at: float | None = None
    cleanup_started_at: float | None = None
    last_activity_at: float | None = None
    _requests: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=8), init=False, compare=False
    )
    _statuses: deque[dict[str, object]] = field(
        default_factory=lambda: deque(maxlen=8), init=False, compare=False
    )
    _lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def launch_environment(self, instance_id: str) -> dict[str, str]:
        """Freeze launch-time identifiers; session transfers only change the current owner."""
        with self._lock:
            self.launch_id = uuid.uuid4().hex
            self.launch_session_id = self.session_id
            self.launched_at = time.time()
            self.exit_observed_at = None
            self.cleanup_started_at = None
            self.last_activity_at = None
            self._requests.clear()
            self._statuses.clear()
            return {
                TERMINAL_INSTANCE_ID_ENV: instance_id,
                TERMINAL_LAUNCH_ID_ENV: self.launch_id,
                TERMINAL_LAUNCH_SESSION_ID_ENV: self.launch_session_id or "",
            }

    def transfer_session(self, session_id: str) -> None:
        """Update the current owner while preserving the running child's launch identity."""
        with self._lock:
            self.session_id = session_id

    def note_request(self, action: str, source: str) -> None:
        """Record a known control request without claiming it caused an exit."""
        with self._lock:
            self._requests.append({"action": action, "source": source, "recorded_at": time.time()})

    def note_status(self, status: str, source: str) -> None:
        """Keep recent status edges and their source, excluding terminal contents."""
        with self._lock:
            if self._statuses:
                previous = self._statuses[-1]
                if previous["status"] == status and previous["source"] == source:
                    return
            self._statuses.append({"status": status, "source": source, "recorded_at": time.time()})

    def note_activity(self) -> None:
        """Remember the last pane change without treating it as a turn boundary."""
        with self._lock:
            self.last_activity_at = time.time()

    def note_exit(self) -> None:
        """Retain the first observation, including when cleanup later runs again."""
        with self._lock:
            if self.exit_observed_at is None:
                self.exit_observed_at = time.time()

    def note_cleanup(self) -> bool:
        """Record cleanup separately from control requests; return whether it is new."""
        with self._lock:
            if self.cleanup_started_at is not None:
                return False
            self.cleanup_started_at = time.time()
            return True

    def snapshot(self) -> dict[str, object]:
        """Copy the bounded history before shutdown can mutate it."""
        with self._lock:
            last_request = self._requests[-1] if self._requests else {}
            return {
                "terminal_launch_id": self.launch_id,
                "terminal_launch_session_id": self.launch_session_id,
                "terminal_current_session_id": self.session_id,
                "terminal_launched_at": self.launched_at,
                "terminal_exit_observed_at": self.exit_observed_at,
                "terminal_cleanup_started_at": self.cleanup_started_at,
                "terminal_last_activity_at": self.last_activity_at,
                "terminal_control_request_action": last_request.get("action"),
                "terminal_control_request_source": last_request.get("source"),
                "terminal_control_requested_at": last_request.get("recorded_at"),
                "terminal_control_requests": list(self._requests),
                "terminal_status_history": list(self._statuses),
            }

    def log_attributes(self) -> dict[str, str]:
        """Return scalars and JSON histories ready for the telemetry boundary."""
        return lifecycle_log_attributes(self.snapshot())
