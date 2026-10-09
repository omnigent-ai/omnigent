"""Small supervisor for the per-user host service.

The service manager owns this process.  The host itself is always a fresh
subprocess so an in-place package upgrade cannot leave the service running old
Python modules.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Sequence
from datetime import date, datetime
from pathlib import Path

from omnigent.process_logging import data_dir

_logger = logging.getLogger(__name__)

MAINTENANCE_HOUR = 4
MAINTENANCE_WINDOW_END_HOUR = 6
UPGRADE_TIMEOUT_SECONDS = 30 * 60
PROCESS_STOP_TIMEOUT_SECONDS = 10.0
SUPERVISOR_POLL_SECONDS = 1.0


def _local_now() -> datetime:
    """Return the current time with the host's local timezone attached."""
    return datetime.now().astimezone()


def _state_path() -> Path:
    """Return the private date marker used by the host service."""
    return data_dir() / "host" / "supervisor-upgrade-date"


def _maintenance_due(now: datetime, last_attempt: date | None) -> bool:
    """Whether today's maintenance attempt should start now.

    A service that wakes after 06:00 skips that day's window.  A service that
    wakes during the window still performs the attempt, which handles sleep or
    a service restart without introducing a second schedule.
    """
    if last_attempt is not None and last_attempt >= now.date():
        return False
    return MAINTENANCE_HOUR <= now.hour < MAINTENANCE_WINDOW_END_HOUR


def _atomic_write_date(path: Path, value: date) -> None:
    """Persist one date without exposing a partially written marker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(value.isoformat())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _read_date(path: Path) -> date | None:
    """Read a marker, treating absent or malformed state as no marker."""
    try:
        return date.fromisoformat(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _command(*parts: str) -> list[str]:
    """Use the same installation in a fresh interpreter."""
    return [sys.executable, "-P", "-m", "omnigent", *parts]


class HostSupervisor:
    """Keep one host child alive and perform one daily package upgrade."""

    def __init__(
        self,
        server_url: str,
        *,
        state_path: Path | None = None,
        now: Callable[[], datetime] = _local_now,
        monotonic: Callable[[], float] = time.monotonic,
        popen: Callable[..., subprocess.Popen[bytes]] = subprocess.Popen,
    ) -> None:
        self.server_url = server_url
        self.state_path = state_path or _state_path()
        self._now = now
        self._monotonic = monotonic
        self._popen = popen
        self._last_attempt = _read_date(self.state_path)
        self._stop_requested = threading.Event()
        self._child: subprocess.Popen[bytes] | None = None
        self._updater: subprocess.Popen[bytes] | None = None

    @property
    def child_command(self) -> list[str]:
        """Return the fresh foreground host command."""
        return _command(
            "host",
            "--server",
            self.server_url,
            "--non-interactive",
            "--no-open",
        )

    @property
    def upgrade_command(self) -> list[str]:
        """Return the existing package updater command."""
        # The host is already stopped; its sessions cannot finish draining.
        return _command("upgrade", "--force")

    def _start(self, args: Sequence[str]) -> subprocess.Popen[bytes] | None:
        """Start a child in its own process group."""
        try:
            return self._popen(
                list(args),
                stdin=subprocess.DEVNULL,
                start_new_session=(os.name == "posix"),
            )
        except OSError:
            _logger.exception("Could not start host service subprocess")
            return None

    def _send_signal(self, process: subprocess.Popen[bytes], signum: int) -> None:
        """Signal a process and its descendants when the platform supports it."""
        if os.name == "posix":
            # Each child starts its own session. Its installer can outlive the
            # group leader, so signal the known group even after that PID exits.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signum)
        elif process.poll() is None:
            with contextlib.suppress(OSError):
                process.send_signal(signum)

    def _stop_process(self, process: subprocess.Popen[bytes]) -> None:
        """Stop a process group within a bounded grace period."""
        self._send_signal(process, signal.SIGTERM)
        try:
            process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        finally:
            # Reap descendants that ignored SIGTERM even if the leader exited.
            self._send_signal(process, getattr(signal, "SIGKILL", signal.SIGTERM))
        process.wait(timeout=PROCESS_STOP_TIMEOUT_SECONDS)

    def _claim_date(self, current: date) -> bool:
        """Record an attempt before touching the running host."""
        if current == self._last_attempt:
            return False
        try:
            _atomic_write_date(self.state_path, current)
        except OSError:
            _logger.exception("Could not persist host upgrade state; skipping upgrade")
            # Avoid a tight retry loop for the remainder of this process.  A
            # later service restart can retry once the state directory works.
            self._last_attempt = current
            return False
        self._last_attempt = current
        return True

    def _run_upgrade(self) -> bool:
        """Run the package updater, stopping it and its descendants on shutdown."""
        process = self._start(self.upgrade_command)
        if process is None:
            return False
        self._updater = process
        deadline = self._monotonic() + UPGRADE_TIMEOUT_SECONDS
        try:
            while process.poll() is None:
                if self._stop_requested.is_set():
                    return False
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    _logger.error(
                        "Host upgrade timed out after %.0f seconds", UPGRADE_TIMEOUT_SECONDS
                    )
                    return False
                try:
                    process.wait(timeout=min(SUPERVISOR_POLL_SECONDS, remaining))
                except subprocess.TimeoutExpired:
                    continue
            code = process.returncode
            if code:
                _logger.error("Host upgrade exited with status %s", code)
                return False
            return True
        finally:
            self._stop_process(process)
            self._updater = None

    def _handle_signal(self, signum: int, _frame: object) -> None:
        """Request shutdown and promptly signal active process groups."""
        self._stop_requested.set()
        if self._child is not None:
            self._send_signal(self._child, signum)
        if self._updater is not None:
            self._send_signal(self._updater, signum)

    def run(self) -> int:
        """Run until the service is stopped or the host crashes/fails fatally."""
        previous_handlers = {}
        for signum in (signal.SIGTERM, signal.SIGINT):
            previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle_signal)
        try:
            child = self._start(self.child_command)
            if child is None:
                return 1
            self._child = child
            while not self._stop_requested.is_set():
                code = child.poll()
                if code is not None:
                    return 128 + -code if code < 0 else code
                now = self._now()
                if _maintenance_due(now, self._last_attempt) and self._claim_date(now.date()):
                    self._stop_process(child)
                    self._child = None
                    if self._stop_requested.is_set():
                        return 0
                    self._run_upgrade()
                    if self._stop_requested.is_set():
                        return 0
                    child = self._start(self.child_command)
                    if child is None:
                        return 1
                    self._child = child
                    continue
                with contextlib.suppress(subprocess.TimeoutExpired):
                    child.wait(timeout=SUPERVISOR_POLL_SECONDS)
            return 0
        finally:
            if self._updater is not None:
                self._stop_process(self._updater)
                self._updater = None
            if self._child is not None:
                self._stop_process(self._child)
                self._child = None
            for signum, handler in previous_handlers.items():
                signal.signal(signum, handler)


def run_supervisor(server_url: str) -> int:
    """Run the service supervisor for *server_url* (empty means local mode)."""
    return HostSupervisor(server_url).run()
