"""Identity-verified process helpers for tests."""

from __future__ import annotations

import contextlib
import os
import signal
import time

from omnigent.inner import _proc

_KILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def capture_identity(pid: int) -> str:
    """Record *pid*'s incarnation identity; fails the test if unreadable."""
    identity = _proc.process_start_identity(pid)
    assert identity is not None, f"could not capture identity of pid {pid}"
    return identity


def settled(pid: int, identity: str) -> bool:
    """Whether the recorded incarnation is DEFINITIVELY dead."""
    state = _proc.process_identity_state(pid, identity)
    if state == "gone":
        return True
    if state == "match" and _proc.process_is_zombie(pid):
        return True
    return False


def alive(pid: int, identity: str) -> bool:
    """Whether the recorded incarnation is NOT yet definitively dead."""
    return not settled(pid, identity)


def wait_gone(pid: int, identity: str, deadline_s: float = 10.0) -> bool:
    """Poll until the recorded incarnation is provably dead."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if settled(pid, identity):
            return True
        time.sleep(0.05)
    return settled(pid, identity)


def reap_adopted(pid: int) -> None:
    """Collect *pid*'s zombie if this process adopted it as a subreaper."""
    with contextlib.suppress(ChildProcessError, OSError):
        os.waitpid(pid, os.WNOHANG)


def safe_kill(pid: int, identity: str) -> None:
    """SIGKILL exactly the recorded incarnation (pidfd-pinned on Linux)."""
    _proc.kill_verified(pid, identity, _KILL)
