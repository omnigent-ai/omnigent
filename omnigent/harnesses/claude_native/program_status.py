"""Prefer live OSC 7501 state while preserving Claude's file fallback."""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

from omnigent.debug_logging import debug_event
from omnigent.harnesses.claude_native.status_file import SessionStatusPoller
from omnigent.terminals.program_status import (
    ProgramStatus,
    ProgramStatusReader,
    blocked_reason,
)

_logger = logging.getLogger(__name__)


class ClaudeProgramStatusPoller:
    """Adapt one terminal's OSC snapshot to existing session-status callbacks."""

    def __init__(
        self,
        reader: ProgramStatusReader,
        fallback: SessionStatusPoller,
        on_status: Callable[[str, str | None], None],
        *,
        omnigent_session_id: str | None = None,
        terminal_instance_id: str | None = None,
        terminal_launch_id: str | None = None,
    ) -> None:
        self._reader = reader
        self._fallback = fallback
        self._on_status = on_status
        self._omnigent_session_id = omnigent_session_id
        self._terminal_instance_id = terminal_instance_id
        self._terminal_launch_id = terminal_launch_id
        self._error_observed = False
        self._last: ProgramStatus | None = None
        self._using_osc = False
        self._retired = False
        self._resync_lock = threading.Lock()
        self._resync_requested = False

    @property
    def active(self) -> bool:
        """Whether OSC or the file owns status instead of pane activity."""
        return not self._retired and (self._reader.status is not None or self._fallback.active)

    @property
    def blocked_on(self) -> str | None:
        """Current blocking reason for native input delivery."""
        if self._retired:
            return None
        report = self._reader.status
        return blocked_reason(report) if report is not None else self._fallback.blocked_on

    def tick(self) -> None:
        """Publish changed reports; never interpret silence as completion."""
        if self._retired:
            return
        with self._resync_lock:
            requested = self._resync_requested
            self._resync_requested = False
        if requested:
            self._last = None
        report = self._reader.status
        is_error = report is not None and report.state == "error"
        # Resync must not turn a standing error into repeated observations.
        if is_error and not self._error_observed:
            _logger.info(
                "Claude program error observed; deferring failed status to StopFailure",
                extra=debug_event(
                    "claude_program_error_observed",
                    session_id=self._omnigent_session_id,
                    terminal_instance_id=self._terminal_instance_id,
                    terminal_launch_id=self._terminal_launch_id,
                    status_source="program_status",
                    status_publication="deferred_to_stop_failure",
                ),
            )
        self._error_observed = is_error
        if report is not None:
            self._using_osc = True
            if report != self._last:
                self._last = report
                # The StopFailure hook owns the failed edge and carries the API
                # error text; an error report here would fail the turn first, bare.
                if report.state != "error":
                    status = "running" if report.state in {"working", "blocked"} else "idle"
                    self._on_status(status, blocked_reason(report))
        else:
            if self._using_osc:
                self._fallback.resync()
            self._using_osc = False
            self._last = None
            self._fallback.tick()

    def resync(self) -> None:
        """Re-arm the next poll after a reconnect or a forwarded idle edge."""
        with self._resync_lock:
            self._resync_requested = True
        self._fallback.resync()

    def retire(self) -> None:
        """Let terminal lifecycle handling own the outcome after process exit."""
        self._retired = True
        self._fallback.retire()
