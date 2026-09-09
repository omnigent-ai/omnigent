"""Run one worker while an inherited parent-liveness pipe remains open."""

from __future__ import annotations

import argparse
import contextlib
import os
import select
import signal
import subprocess
import time


def _group_members(pgid: int) -> list[int]:
    try:
        import psutil  # type: ignore[import-untyped]

        return [
            proc.pid
            for proc in psutil.process_iter(["pid"])
            if proc.pid > 1 and proc.pid != os.getpid() and _safe_getpgid(proc.pid) == pgid
        ]
    except Exception:  # noqa: BLE001 - shutdown must remain best-effort
        return []


def _safe_getpgid(pid: int) -> int | None:
    try:
        return os.getpgid(pid)
    except OSError:
        return None


def _signal_members(pgid: int, sig: int) -> None:
    for pid in _group_members(pgid):
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.kill(pid, sig)


def _terminate_group(
    pgid: int,
    child: subprocess.Popen[bytes],
    *,
    detached_reaper: bool = False,
) -> None:
    if detached_reaper and pgid == os.getpid() and hasattr(os, "fork"):
        try:
            reaper_pid = os.fork()
        except OSError:
            # Resource exhaustion can make fork unavailable exactly when
            # teardown is most important. Fall through to the bounded inline
            # TERM/KILL path rather than abandoning the worker group.
            reaper_pid = -1
        if reaper_pid == 0:
            with contextlib.suppress(OSError):
                os.setsid()
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, signal.SIGTERM)
            time.sleep(2.0)
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, getattr(signal, "SIGKILL", signal.SIGTERM))
            os._exit(0)
        if reaper_pid > 0:
            # The detached reaper now owns bounded escalation. Do not return
            # and accidentally let this group leader exit before TERM is
            # delivered.
            while True:
                signal.pause()
    _signal_members(pgid, signal.SIGTERM)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and _group_members(pgid):
        time.sleep(0.02)
    _signal_members(pgid, getattr(signal, "SIGKILL", signal.SIGTERM))
    with contextlib.suppress(Exception):
        child.wait(timeout=1)


def main() -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--liveness-fd", required=True, type=int)
    parser.add_argument("argv", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if not args.argv:
        return 2
    pgid = os.getpgrp()
    child = subprocess.Popen(args.argv, close_fds=True)
    while child.poll() is None:
        try:
            readable, _, _ = select.select([args.liveness_fd], [], [], 0.1)
            if readable and not os.read(args.liveness_fd, 1):
                _terminate_group(pgid, child, detached_reaper=True)
                return 1
        except OSError:
            _terminate_group(pgid, child, detached_reaper=True)
            return 1
    returncode = child.returncode
    assert returncode is not None
    # A worker leader can exit while descendants remain. Reap the group before
    # returning, without ever signaling the runner's separate process group.
    members = _group_members(pgid)
    if members:
        _signal_members(pgid, signal.SIGTERM)
        time.sleep(0.1)
        _signal_members(pgid, getattr(signal, "SIGKILL", signal.SIGTERM))
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
