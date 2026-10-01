"""Run one worker while an inherited parent-liveness pipe remains open."""

from __future__ import annotations

import argparse
import contextlib
import os
import select
import signal
import subprocess
import time

import psutil  # type: ignore[import-untyped]


def _group_members(pgid: int) -> list[int]:
    try:
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


def _refresh_census(
    child: subprocess.Popen[bytes],
    census: dict[int, float],
) -> None:
    """Remember currently observable descendants by PID and creation time."""
    try:
        root = psutil.Process(child.pid)
        root_create_time = root.create_time()
        expected_root = census.get(child.pid)
        if expected_root is not None and root_create_time != expected_root:
            return
        processes = [root, *root.children(recursive=True)]
    except (psutil.NoSuchProcess, psutil.AccessDenied, TypeError):
        return
    for proc in processes:
        if proc.pid <= 1 or proc.pid == os.getpid():
            continue
        with contextlib.suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            create_time = proc.create_time()
            expected = census.get(proc.pid)
            if expected is None:
                census[proc.pid] = create_time


def _signal_census(census: dict[int, float], sig: int) -> None:
    """Signal only processes whose creation identity still matches."""
    for pid, create_time in census.items():
        if pid <= 1 or pid == os.getpid():
            continue
        try:
            proc = psutil.Process(pid)
            if proc.create_time() != create_time:
                continue
            proc.send_signal(sig)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue


def _census_alive(census: dict[int, float]) -> bool:
    for pid, create_time in census.items():
        try:
            proc = psutil.Process(pid)
            if proc.create_time() == create_time and proc.status() != psutil.STATUS_ZOMBIE:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def _terminate_group(
    pgid: int,
    child: subprocess.Popen[bytes],
    *,
    detached_reaper: bool = False,
    census: dict[int, float] | None = None,
) -> None:
    owned = dict(census or {})
    _refresh_census(child, owned)
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
            _signal_census(owned, signal.SIGTERM)
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                # The detached copy keeps observing the worker while TERM is
                # in flight so a child that calls setsid during shutdown is
                # added before the final KILL sweep.
                _refresh_census(child, owned)
                _signal_census(owned, signal.SIGTERM)
                time.sleep(0.02)
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pgid, getattr(signal, "SIGKILL", signal.SIGTERM))
            _signal_census(owned, getattr(signal, "SIGKILL", signal.SIGTERM))
            os._exit(0)
        if reaper_pid > 0:
            # The detached reaper now owns bounded escalation. Do not return
            # and accidentally let this group leader exit before TERM is
            # delivered.
            while True:
                signal.pause()
    _signal_members(pgid, signal.SIGTERM)
    _signal_census(owned, signal.SIGTERM)
    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and (_group_members(pgid) or _census_alive(owned)):
        _refresh_census(child, owned)
        time.sleep(0.02)
    _signal_members(pgid, getattr(signal, "SIGKILL", signal.SIGTERM))
    _signal_census(owned, getattr(signal, "SIGKILL", signal.SIGTERM))
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
    census: dict[int, float] = {}
    while child.poll() is None:
        _refresh_census(child, census)
        try:
            readable, _, _ = select.select([args.liveness_fd], [], [], 0.1)
            if readable and not os.read(args.liveness_fd, 1):
                _terminate_group(pgid, child, detached_reaper=True, census=census)
                return 1
        except OSError:
            _terminate_group(pgid, child, detached_reaper=True, census=census)
            return 1
    returncode = child.returncode
    assert returncode is not None
    # A worker leader can exit while descendants remain. Reap the group before
    # returning, without ever signaling the runner's separate process group.
    members = _group_members(pgid)
    if members or _census_alive(census):
        _signal_members(pgid, signal.SIGTERM)
        _signal_census(census, signal.SIGTERM)
        time.sleep(0.1)
        _signal_members(pgid, getattr(signal, "SIGKILL", signal.SIGTERM))
        _signal_census(census, getattr(signal, "SIGKILL", signal.SIGTERM))
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
