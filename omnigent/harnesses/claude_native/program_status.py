"""Prefer live OSC 7501 state while preserving Claude's file fallback."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

from omnigent.debug_logging import debug_event
from omnigent.harnesses.claude_native.status_file import SessionStatusPoller
from omnigent.terminals.program_status import (
    ProgramStatus,
    ProgramStatusReader,
    blocked_reason,
)

_logger = logging.getLogger(__name__)

# How long an error report may stand before its turn is failed without the
# StopFailure hook. The hook normally follows within about a second.
ERROR_HOOK_GRACE_S = 5.0


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
        turn_is_active: Callable[[], bool] | None = None,
        turn_activity_epoch: Callable[[], int] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._reader = reader
        self._fallback = fallback
        self._on_status = on_status
        self._omnigent_session_id = omnigent_session_id
        self._terminal_instance_id = terminal_instance_id
        self._terminal_launch_id = terminal_launch_id
        self._error_observed = False
        self._turn_is_active = turn_is_active
        self._turn_activity_epoch = turn_activity_epoch
        self._error_activity_epoch: int | None = None
        self._clock = clock
        self._last: ProgramStatus | None = None
        self._error_report: ProgramStatus | None = None
        self._error_since: float | None = None
        self._error_failed = False
        self._using_osc = False
        self._retired = False
        self._resync_lock = threading.Lock()
        self._resync_requested = False

    @property
    def active(self) -> bool:
        """Whether OSC or the file owns status instead of pane activity."""
        if self._retired:
            return False
        active = self._reader.status is not None or self._fallback.active
        return active and not self._retired

    @property
    def blocked_on(self) -> str | None:
        """Current blocking reason for native input delivery."""
        if self._retired:
            return None
        report = self._reader.status
        if self._retired:
            return None
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
        if self._retired:
            return
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
            self._fail_if_hook_overdue(report)
        else:
            if self._using_osc:
                self._fallback.resync()
            self._using_osc = False
            self._last = None
            self._fail_if_hook_overdue(None)
            self._fallback.tick()

    def _fail_if_hook_overdue(self, report: ProgramStatus | None) -> None:
        """Fail an error turn whose StopFailure hook is overdue, once.

        The runner counts a turn in flight until a hook edge clears it, so an
        error report that stands past the grace means the hook was lost.
        """
        if (
            report is None
            or report.state != "error"
            or self._turn_is_active is None
            or not self._turn_is_active()
        ):
            self._error_report = None
            self._error_since = None
            self._error_activity_epoch = None
            self._error_failed = False
            return
        now = self._clock()
        epoch = self._turn_activity_epoch() if self._turn_activity_epoch is not None else None
        if (
            report != self._error_report
            or self._error_since is None
            or epoch != self._error_activity_epoch
        ):
            self._error_report = report
            self._error_since = now
            self._error_activity_epoch = epoch
            self._error_failed = False
        elif not self._error_failed and now - self._error_since >= ERROR_HOOK_GRACE_S:
            self._error_failed = True
            self._on_status("failed", None)

    def resync(self) -> None:
        """Re-arm the next poll after a reconnect or a forwarded idle edge."""
        with self._resync_lock:
            self._resync_requested = True
        self._fallback.resync()

    def retire(self) -> None:
        """Let terminal lifecycle handling own the outcome after process exit."""
        self._retired = True
        self._fallback.retire()
