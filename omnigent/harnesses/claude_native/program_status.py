"""Prefer live OSC 7501 state while preserving Claude's file fallback."""

from __future__ import annotations

import threading
from collections.abc import Callable

from omnigent.harnesses.claude_native.status_file import SessionStatusPoller
from omnigent.terminals.program_status import (
    ProgramStatus,
    ProgramStatusReader,
    blocked_reason,
)


class ClaudeProgramStatusPoller:
    """Adapt one terminal's OSC snapshot to existing session-status callbacks."""

    def __init__(
        self,
        reader: ProgramStatusReader,
        fallback: SessionStatusPoller,
        on_status: Callable[[str, str | None], None],
    ) -> None:
        self._reader = reader
        self._fallback = fallback
        self._on_status = on_status
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
        if report is not None:
            self._using_osc = True
            if report != self._last:
                self._last = report
                status = (
                    "running"
                    if report.state in {"working", "blocked"}
                    else "failed"
                    if report.state == "error"
                    else "idle"
                )
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
