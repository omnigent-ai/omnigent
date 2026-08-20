"""Host-side client for the runner zygote forkserver.

The host daemon talks to a single long-lived zygote process
(:mod:`omnigent.runner._zygote`) over an ``AF_UNIX`` socketpair. The zygote
imports the runner graph once and ``os.fork()``s a child per session, so the
~120MB import floor is shared copy-on-write instead of paid per runner.

This module owns:

* :class:`ZygoteManager` — lazily spawns the zygote (a plain ``Popen`` of a
  fresh interpreter, so it never inherits the daemon's asyncio loop / threads),
  and exposes ``fork_runner`` plus the poll/terminate/kill primitives the
  daemon needs.
* :class:`ZygoteRunnerProc` — a minimal stand-in for ``subprocess.Popen`` that
  ``_RunnerHandle`` and the daemon's watch/stop/cleanup paths use unchanged. The
  daemon is NOT the forked runner's OS parent, so ``poll``/``returncode``/
  ``wait`` round-trip to the zygote (the real parent) for exit status, while
  ``terminate``/``kill`` signal the pid directly.

This code runs on any POSIX host (the gate is ``IS_POSIX``); the copy-on-write
*savings* are Linux-specific, but the fork/protocol path executes on macOS too.
On by default; set ``OMNIGENT_RUNNER_ZYGOTE=0`` to opt out. The daemon falls
back to direct ``Popen`` on any failure, so the zygote is never a hard
dependency.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import BinaryIO

from omnigent.inner import _proc
from omnigent.process_logging import child_logging_popen_kwargs
from omnigent.runner._zygote import ZYGOTE_CONTROL_FD_ENV_VAR

logger = logging.getLogger(__name__)

# Bound each control round-trip so a wedged zygote can never hang the daemon;
# fork + reply is sub-millisecond locally, so this is purely a safety net.
_CONTROL_TIMEOUT_S = 30.0

# Reported for a forked runner whose real exit code can no longer be recovered
# because the zygote (its OS parent, the only process that could waitpid it)
# died. The runner process itself is confirmed gone by a direct pid probe; we
# just can't know its code, so surface a non-zero sentinel — a lost runner must
# read as dead-and-failed, never as a clean exit or an eternal "still alive".
_ZYGOTE_LOST_EXIT_CODE = 254


class ZygoteUnavailable(RuntimeError):
    """The zygote could not be started or stopped responding.

    The daemon catches this and falls back to a direct ``Popen`` launch.
    """


class ZygoteRunnerProc:
    """A ``subprocess.Popen``-shaped handle for a zygote-forked runner.

    Implements only the surface the daemon uses on a runner handle: ``pid``,
    ``poll()``, ``returncode``, ``wait()``, ``terminate()``, ``kill()``. Exit
    status comes from the zygote (the real parent); signals go to the pid.

    :param pid: The forked runner's process id.
    :param manager: The owning :class:`ZygoteManager`, used to poll exit status.
    """

    def __init__(self, pid: int, manager: ZygoteManager) -> None:
        self.pid = pid
        self._manager = manager
        self.returncode: int | None = None

    def poll(self) -> int | None:
        """Return the exit code if the runner has exited, else ``None``.

        Caches the first observed exit code in :attr:`returncode`, matching
        ``subprocess.Popen.poll`` semantics the daemon relies on.
        """
        if self.returncode is not None:
            return self.returncode
        code = self._manager.poll(self.pid)
        if code is not None:
            self.returncode = code
        return self.returncode

    def wait(self, timeout: float | None = None) -> int:
        """Block until the runner exits or *timeout* elapses.

        :param timeout: Max seconds to wait; ``None`` waits indefinitely.
        :returns: The runner exit code.
        :raises subprocess.TimeoutExpired: If *timeout* elapses first.
        """
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            code = self.poll()
            if code is not None:
                return code
            if timeout is not None and deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(cmd="omnigent.runner._zygote", timeout=timeout)
            time.sleep(0.05)

    def terminate(self) -> None:
        """Send ``SIGTERM`` to the runner (best-effort)."""
        self._signal(signal.SIGTERM)

    def kill(self) -> None:
        """Send ``SIGKILL`` to the runner (best-effort)."""
        self._signal(getattr(signal, "SIGKILL", signal.SIGTERM))

    def _signal(self, sig: int) -> None:
        """Signal the runner pid, ignoring an already-exited process.

        :param sig: POSIX signal number.
        """
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(self.pid, sig)


class ZygoteManager:
    """Owns the daemon's connection to the runner zygote forkserver.

    Thread-safe: ``fork_runner`` is typically driven from ``asyncio.to_thread``
    while ``poll`` runs on the event-loop thread, so every control exchange is
    serialized under one lock.

    :param python_executable: Interpreter to launch the zygote with; defaults
        to ``sys.executable``.
    :param log_path: Optional file for the zygote's own stdout/stderr.
    """

    def __init__(
        self,
        *,
        python_executable: str | None = None,
        log_path: Path | None = None,
    ) -> None:
        self._python = python_executable or sys.executable
        self._log_path = log_path
        self._proc: subprocess.Popen[bytes] | None = None
        self._sock: socket.socket | None = None
        self._lock = threading.Lock()

    @property
    def pid(self) -> int | None:
        """The zygote process pid, or ``None`` if not started."""
        return self._proc.pid if self._proc is not None else None

    def is_running(self) -> bool:
        """Whether the zygote process is started and has not exited."""
        return self._proc is not None and self._proc.poll() is None

    def start(self) -> None:
        """Spawn the zygote if it is not already running.

        The zygote is a fresh ``Popen`` interpreter (never a fork of this
        multithreaded async daemon). One end of an ``AF_UNIX`` socketpair is
        passed to it via ``pass_fds``; the daemon keeps the other end.

        :raises ZygoteUnavailable: If the process could not be spawned or does
            not answer an initial ping.
        """
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            parent_sock, child_sock = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                proc = self._spawn_zygote_process(child_sock.fileno())
            except OSError as exc:
                parent_sock.close()
                child_sock.close()
                raise ZygoteUnavailable(f"failed to spawn runner zygote: {exc}") from exc
            # The child owns its copy of child_fd now; close ours.
            child_sock.close()
            parent_sock.settimeout(_CONTROL_TIMEOUT_S)
            self._proc = proc
            self._sock = parent_sock

        # Confirm the zygote is answering before the caller relies on it. Any
        # failure here (bad pong, or the exchange itself raising on a timeout /
        # EOF) must tear down the partially-started zygote so a failed start
        # never leaks a process + socket.
        try:
            reply = self._exchange({"cmd": "ping"})
            answered = bool(reply.get("pong"))
        except ZygoteUnavailable:
            self.stop()
            raise
        if not answered:
            self.stop()
            raise ZygoteUnavailable("runner zygote did not answer ping")

    def _spawn_zygote_process(self, child_fd: int) -> subprocess.Popen[bytes]:
        """Popen the zygote interpreter, handing it *child_fd* as its control fd.

        :param child_fd: The child end of the control socketpair to pass through.
        :returns: The spawned zygote process.
        :raises OSError: If the interpreter could not be spawned.
        """
        env = {**os.environ, ZYGOTE_CONTROL_FD_ENV_VAR: str(child_fd)}
        # glibc reads these once, when its allocator initializes at exec. This is
        # the only exec on the zygote path — forked runners just replace
        # os.environ, far too late to matter — so the cap must be applied here to
        # reach every child. setdefault keeps an operator override winning.
        for key, value in _proc.malloc_tuning_env().items():
            env.setdefault(key, value)
        with contextlib.ExitStack() as stack:
            # The zygote's own stdout/stderr go to its log file when configured,
            # else inherit the daemon's; stdin is always /dev/null.
            log_fh: BinaryIO | None = None
            if self._log_path is not None:
                log_fh = stack.enter_context(open(self._log_path, "ab", buffering=0))
            # Forward the --log-to-stderr terminal fd so runners forked by the
            # zygote can mirror to the daemon's TTY exactly like the direct-Popen
            # path. The helper dups the fd, rewrites env[LOG_TTY_FD] to the duped
            # number, and yields it in pass_fds; the zygote then propagates that
            # (zygote-local) number into each forked child's env.
            tty_kwargs = stack.enter_context(child_logging_popen_kwargs(env))
            pass_fds = (child_fd, *tty_kwargs.get("pass_fds", ()))
            return subprocess.Popen(
                # -P keeps the daemon's cwd off sys.path, so a daemon started
                # inside an omnigent checkout can't hand forked runners a
                # different omnigent than the installed one.
                [self._python, "-P", "-m", "omnigent.runner._zygote"],
                env=env,
                pass_fds=pass_fds,
                stdin=subprocess.DEVNULL,
                stdout=log_fh,
                stderr=log_fh,
            )

    def fork_runner(self, env: dict[str, str], log_path: str, workspace: str) -> ZygoteRunnerProc:
        """Ask the zygote to fork a runner with *env*, returning its handle.

        :param env: Full runner environment (the child replaces ``os.environ``
            with it). Must already carry ``RUNNER_PARENT_PID`` set to the
            zygote's pid so the runner's orphan watchdog stays correct.
        :param log_path: Session log path the child points stdout/stderr at.
        :param workspace: Existing session workspace to use as the child's cwd.
            Required: a daemon may outlive the checkout it started from, so
            inheriting its cwd would make every ``Path.cwd()`` in the runner
            raise ``FileNotFoundError``.
        :returns: A :class:`ZygoteRunnerProc` for the forked runner.
        :raises ZygoteUnavailable: If the zygote is down or reports a fork error.
        """
        reply = self._exchange(
            {
                "cmd": "fork",
                "env": env,
                "log_path": log_path,
                "cwd": workspace,
            }
        )
        if "error" in reply:
            raise ZygoteUnavailable(f"zygote fork failed: {reply['error']}")
        pid = reply.get("pid")
        if not isinstance(pid, int):
            raise ZygoteUnavailable(f"zygote returned no pid: {reply!r}")
        return ZygoteRunnerProc(pid, self)

    def poll(self, pid: int) -> int | None:
        """Return the exit code for a forked runner, or ``None`` if still live.

        :param pid: The forked runner pid.
        :returns: Exit code once the zygote has reaped it, else ``None``.
        """
        try:
            reply = self._exchange({"cmd": "poll", "pid": pid})
        except ZygoteUnavailable:
            # The zygote (the runner's OS parent) died, so exit status can no
            # longer be recovered over the control socket. Probe the runner pid
            # directly: if it is gone, surface a dead-and-failed sentinel so the
            # daemon's watcher stops looping and reports the exit; if it is
            # somehow still alive, report "still live" and keep polling. Never
            # report an eternal None for a process that has actually exited.
            return None if _proc.process_alive(pid) else _ZYGOTE_LOST_EXIT_CODE
        code = reply.get("returncode")
        return code if isinstance(code, int) else None

    def stop(self) -> None:
        """Terminate the zygote and close the control socket.

        Its forked runner children reparent and self-terminate via their orphan
        watchdog, so no runner is left stranded.
        """
        with self._lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                finally:
                    self._sock = None
            if self._proc is not None:
                if self._proc.poll() is None:
                    self._proc.terminate()
                    try:
                        self._proc.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        self._proc.kill()
                        # Reap after SIGKILL so the zygote doesn't linger as a
                        # zombie until the daemon exits / the Popen is GC'd.
                        with contextlib.suppress(subprocess.TimeoutExpired):
                            self._proc.wait(timeout=5.0)
                self._proc = None

    def _exchange(self, request: dict[str, object]) -> dict[str, object]:
        """Send one request and read one JSON reply under the control lock.

        :param request: The protocol request (``ping`` / ``fork`` / ``poll``).
        :returns: The decoded reply.
        :raises ZygoteUnavailable: On any socket error or if the zygote closed.
        """
        with self._lock:
            sock = self._sock
            if sock is None:
                raise ZygoteUnavailable("runner zygote is not running")
            try:
                sock.sendall(json.dumps(request).encode("utf-8") + b"\n")
                return self._read_reply(sock)
            except (OSError, ValueError) as exc:
                raise ZygoteUnavailable(f"runner zygote control error: {exc}") from exc

    @staticmethod
    def _read_reply(sock: socket.socket) -> dict[str, object]:
        """Read one newline-delimited JSON reply from *sock*.

        :param sock: The connected control socket.
        :returns: The decoded reply mapping.
        :raises ZygoteUnavailable: On EOF (zygote exited) before a full line.
        :raises OSError: On a socket-level failure (surfaced by the caller).
        """
        buf = bytearray()
        while b"\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise ZygoteUnavailable("runner zygote closed the control socket")
            buf.extend(chunk)
        line, _, _ = bytes(buf).partition(b"\n")
        decoded = json.loads(line)
        if not isinstance(decoded, dict):
            raise ZygoteUnavailable(f"runner zygote sent a non-object reply: {decoded!r}")
        return decoded
