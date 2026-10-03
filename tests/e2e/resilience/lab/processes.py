"""Subprocess ownership and process-tree freezing for the resilience lab."""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Iterable, Iterator, Sequence
from pathlib import Path

import psutil

from tests.e2e.resilience.lab.events import EventLog


class ManagedProcess:
    """One lab-owned subprocess in its own process group, logging to a file.

    :param name: Component name, e.g. ``"server"``.
    :param argv: Command line, e.g. ``[sys.executable, "-m", "omnigent", "server"]``.
    :param env: Complete environment for the child.
    :param cwd: Working directory.
    :param log_path: File receiving stdout and stderr; appended across restarts.
    :param events: Lab event log.
    """

    def __init__(
        self,
        name: str,
        argv: Sequence[str],
        *,
        env: dict[str, str],
        cwd: Path,
        log_path: Path,
        events: EventLog,
    ) -> None:
        self.name = name
        self.argv = list(argv)
        self.env = env
        self.cwd = cwd
        self.log_path = log_path
        self._events = events
        self._proc: subprocess.Popen[bytes] | None = None

    @property
    def pid(self) -> int:
        """PID of the current generation."""
        if self._proc is None:
            raise RuntimeError(f"{self.name} not started")
        return self._proc.pid

    @property
    def running(self) -> bool:
        """Whether the current generation is alive."""
        return self._proc is not None and self._proc.poll() is None

    @property
    def returncode(self) -> int | None:
        """Exit code of the current generation, or ``None`` while running."""
        return None if self._proc is None else self._proc.poll()

    def start(self) -> None:
        """Start a new generation; the previous one must have exited."""
        if self.running:
            raise RuntimeError(f"{self.name} is already running")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        with self.log_path.open("ab") as log:
            self._proc = subprocess.Popen(
                self.argv,
                env=self.env,
                cwd=self.cwd,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        self._events.emit("lab", "process_start", process=self.name, pid=self._proc.pid)

    def stop(self, *, graceful: bool = True, timeout: float = 15.0) -> None:
        """Stop the process group, escalating to SIGKILL after *timeout*.

        :param graceful: Send SIGTERM first; ``False`` sends SIGKILL at once.
        :param timeout: Seconds to wait after SIGTERM, e.g. ``15.0``.
        """
        proc = self._proc
        if proc is None or proc.poll() is not None:
            return
        self._events.emit(
            "lab", "process_stop", process=self.name, pid=proc.pid, graceful=graceful
        )
        if graceful:
            self._signal_group(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=timeout)
                return
            except subprocess.TimeoutExpired:
                pass
        self._signal_group(proc.pid, signal.SIGKILL)
        proc.wait(timeout=10)

    def log_tail(self, chars: int = 4000) -> str:
        """Return the end of the process log.

        :param chars: Maximum characters, e.g. ``4000``.
        :returns: The log tail, or ``""`` before the first start.
        """
        if not self.log_path.exists():
            return ""
        return self.log_path.read_text(errors="replace")[-chars:]

    @staticmethod
    def _signal_group(pid: int, sig: signal.Signals) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, sig)


def process_tree(pids: Iterable[int]) -> list[psutil.Process]:
    """Return each live PID with all of its descendants, without duplicates.

    :param pids: Root PIDs, e.g. ``[12345]``.
    :returns: Live processes, roots before their descendants.
    """
    seen: dict[int, psutil.Process] = {}
    for pid in pids:
        try:
            root = psutil.Process(pid)
            members = [root, *root.children(recursive=True)]
        except psutil.NoSuchProcess:
            continue
        for member in members:
            seen.setdefault(member.pid, member)
    return list(seen.values())


def processes_mentioning(marker: str) -> list[psutil.Process]:
    """Return this user's processes whose command line contains *marker*.

    Finds daemonized helpers (for example a tmux server whose socket lives in
    a lab directory) that left the lab's process tree.

    :param marker: Substring, e.g. a lab temp directory path.
    :returns: Matching processes.
    """
    found: list[psutil.Process] = []
    me = psutil.Process().username()
    for proc in psutil.process_iter(["cmdline", "username"]):
        try:
            if proc.info["username"] != me:
                continue
            if any(marker in part for part in proc.info["cmdline"] or ()):
                found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return found


@contextlib.contextmanager
def frozen(processes: Sequence[psutil.Process], events: EventLog) -> Iterator[None]:
    """SIGSTOP *processes* for the duration of the block, then SIGCONT them.

    :param processes: Processes to freeze.
    :param events: Lab event log.
    """
    stopped: list[psutil.Process] = []
    try:
        for proc in processes:
            with contextlib.suppress(psutil.NoSuchProcess):
                proc.send_signal(signal.SIGSTOP)
                stopped.append(proc)
        events.emit("lab", "freeze", pids=[p.pid for p in stopped])
        yield
    finally:
        for proc in reversed(stopped):
            with contextlib.suppress(psutil.NoSuchProcess):
                proc.send_signal(signal.SIGCONT)
        events.emit("lab", "thaw", pids=[p.pid for p in stopped])
