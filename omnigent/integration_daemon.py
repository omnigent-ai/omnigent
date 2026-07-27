"""Background-daemon lifecycle for CLI-managed integration processes.

Backs ``omni integration slack [--background|status|stop|logs]``. A single daemon
per machine is tracked by a small JSON record (PID + log path + start time)
under the runtime data dir. The daemon itself is an ordinary subprocess (e.g.
``python -m omnigent_slack``); this module only owns spawning it detached,
recording it, checking liveness, and tearing it down — it holds no
integration-specific knowledge.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from omnigent.inner import _proc


@dataclass(frozen=True, slots=True)
class DaemonRecord:
    """A running (or last-known) background daemon.

    :param pid: Spawned process id.
    :param log_path: Absolute path to the daemon's combined stdout/stderr log.
    :param started_at: Unix epoch seconds when the daemon was spawned.
    """

    pid: int
    log_path: str
    started_at: int


class IntegrationDaemon:
    """Manage one named background daemon tracked by a PID record.

    :param name: Stable identifier, e.g. ``"slack"``. Names the record file,
        the log destination, and user-facing messages.
    :param state_dir: Directory the record lives in (honors the caller's
        data-dir resolution, so tests can isolate via ``OMNIGENT_DATA_DIR``).
    """

    def __init__(self, name: str, state_dir: Path) -> None:
        self.name = name
        self._record_path = state_dir / "integrations" / f"{name}.json"

    # ── Record persistence ────────────────────────────────────────

    def read_record(self) -> DaemonRecord | None:
        """Return the recorded daemon, or ``None`` if absent/malformed."""
        try:
            raw = json.loads(self._record_path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(raw, dict):
            return None
        try:
            pid = int(raw["pid"])
            log_path = str(raw["log_path"])
            started_at = int(raw["started_at"])
        except (KeyError, TypeError, ValueError):
            return None
        return DaemonRecord(pid=pid, log_path=log_path, started_at=started_at)

    def _write_record(self, record: DaemonRecord) -> None:
        self._record_path.parent.mkdir(parents=True, exist_ok=True)
        self._record_path.write_text(
            json.dumps(
                {
                    "pid": record.pid,
                    "log_path": record.log_path,
                    "started_at": record.started_at,
                }
            )
        )

    def _clear_record(self) -> None:
        self._record_path.unlink(missing_ok=True)

    # ── Liveness ──────────────────────────────────────────────────

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        """Whether *pid* names a live process (best-effort, POSIX/Windows)."""
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            # Exists but owned by another user — still "alive" for our purposes.
            return True
        except OSError:
            return False
        return True

    def running_record(self) -> DaemonRecord | None:
        """Return the record only if its process is actually alive.

        Prunes a stale record (process gone) as a side effect so ``status``
        and ``start`` never act on a dead PID.
        """
        record = self.read_record()
        if record is None:
            return None
        if not self._pid_alive(record.pid):
            self._clear_record()
            return None
        return record

    def confirm_alive(self, record: DaemonRecord, *, grace_seconds: float) -> bool:
        """Return whether *record*'s process survives ``grace_seconds``.

        A detached daemon that dies on startup (e.g. missing config) leaves
        no signal on the terminal — the caller uses this to turn that silent
        failure into a visible error. Checks liveness immediately, then polls
        until the grace elapses; if the process is gone the stale record is
        pruned and ``False`` is returned.
        """
        deadline = time.time() + grace_seconds
        while True:
            if not self._pid_alive(record.pid):
                self._clear_record()
                return False
            if time.time() >= deadline:
                return True
            time.sleep(0.1)

    def read_log_tail(self, max_lines: int = 20) -> str:
        """Return the last ``max_lines`` of the daemon's log (best-effort)."""
        record = self.read_record()
        if record is None:
            return ""
        try:
            lines = Path(record.log_path).read_text(errors="replace").splitlines()
        except OSError:
            return ""
        return "\n".join(lines[-max_lines:])

    # ── Lifecycle ─────────────────────────────────────────────────

    def start(
        self, argv: list[str], env: dict[str, str], *, cwd: Path | None = None
    ) -> DaemonRecord:
        """Spawn *argv* detached, record it, and return the record.

        Reuses the harness's detached-spawn kwargs (new session/process
        group) and combined-log capture, mirroring the host daemon. The
        caller is responsible for the already-running check.

        :param cwd: Working directory for the child. Set this when the
            integration resolves config from a CWD-relative path (e.g. a
            ``.env`` file) so the daemon doesn't inherit the arbitrary
            directory ``omni`` was launched from.
        """
        from omnigent.process_logging import (
            PROCESS_LOG_FILE_ENV_VAR,
            child_logging_popen_kwargs,
            open_process_log_file,
        )

        log_path, log_fh = open_process_log_file(self.name)
        env = {**env, PROCESS_LOG_FILE_ENV_VAR: str(log_path)}
        # Detached: own session/process group (spawn_kwargs), stdin closed,
        # stdout+stderr to the log file. Mirrors the host daemon spawn.
        try:
            with child_logging_popen_kwargs(env) as logging_kwargs:
                # spawn_kwargs()/logging_kwargs are dict[str, object] splats, so
                # mypy can't resolve a Popen overload; the runtime kwargs are
                # valid (matches the host-daemon spawn).
                proc = subprocess.Popen(  # type: ignore[call-overload]
                    argv,
                    env=env,
                    cwd=str(cwd) if cwd is not None else None,
                    stdin=subprocess.DEVNULL,
                    stdout=log_fh,
                    stderr=log_fh,
                    **_proc.spawn_kwargs(),
                    **logging_kwargs,
                )
        finally:
            log_fh.close()
        record = DaemonRecord(pid=proc.pid, log_path=str(log_path), started_at=int(time.time()))
        self._write_record(record)
        return record

    def stop(self, *, grace_seconds: float = 5.0) -> DaemonRecord | None:
        """Terminate the running daemon; return the stopped record or ``None``.

        Sends SIGTERM to the daemon's process group (it was spawned in its
        own session), waits up to ``grace_seconds``, then escalates to
        SIGKILL. Idempotent: a missing/dead daemon clears the record and
        returns ``None``.
        """
        record = self.running_record()
        if record is None:
            self._clear_record()
            return None
        self._signal(record.pid, signal.SIGTERM)
        deadline = time.time() + grace_seconds
        while time.time() < deadline:
            if not self._pid_alive(record.pid):
                break
            time.sleep(0.1)
        if self._pid_alive(record.pid):
            self._signal(record.pid, getattr(signal, "SIGKILL", signal.SIGTERM))
        self._clear_record()
        return record

    @staticmethod
    def _signal(pid: int, sig: int) -> None:
        """Signal the daemon's process group, falling back to the bare PID."""
        try:
            killpg = getattr(os, "killpg", None)
            if killpg is not None:
                killpg(os.getpgid(pid), sig)
            else:
                os.kill(pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            # Best-effort: fall back to a direct signal, ignore if already gone.
            with contextlib.suppress(OSError):
                os.kill(pid, sig)
