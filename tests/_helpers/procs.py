"""Identity-verified process helpers for tests.

Tests must never observe or signal a NON-CHILD pid through raw
primitives: on a busy host the pid can be recycled mid-test, making a
liveness poll watch a stranger forever and a cleanup ``os.kill`` shoot
one. These wrappers speak the same identity vocabulary as production
(:mod:`omnigent.inner._proc`). A test's own unreaped ``Popen`` child is
exempt — holding the handle pins the pid — and should keep using
``poll()``/``kill()``/``wait()``.
"""

from __future__ import annotations

import signal
import time

from omnigent.inner import _proc

_KILL = getattr(signal, "SIGKILL", signal.SIGTERM)


def capture_identity(pid: int) -> str:
    """Record *pid*'s incarnation identity; fails the test if unreadable.

    :param pid: A live process the test just learned about.
    :returns: The identity string for later verified waits/kills.
    """
    identity = _proc.process_start_identity(pid)
    assert identity is not None, f"could not capture identity of pid {pid}"
    return identity


def settled(pid: int, identity: str) -> bool:
    """Whether the recorded incarnation is DEFINITIVELY dead.

    Death is proven only by identity ``"gone"`` or by the same incarnation
    (``"match"``) being an unreaped zombie. ``"unverifiable"`` (exists but
    unreadable — a foreign/racing process) is NOT death: it keeps waiting.

    :param pid: The recorded pid.
    :param identity: Its captured identity.
    :returns: ``True`` only on positive proof of death.
    """
    state = _proc.process_identity_state(pid, identity)
    if state == "gone":
        return True
    if state == "match" and _proc.process_is_zombie(pid):
        return True
    return False


def alive(pid: int, identity: str) -> bool:
    """Whether the recorded incarnation is NOT yet definitively dead.

    The inverse of :func:`settled`: an ``"unverifiable"`` process counts as
    still-alive so waiters keep waiting rather than declaring a possibly
    live process gone.

    :param pid: The recorded pid.
    :param identity: Its captured identity.
    :returns: ``True`` until death is proven.
    """
    return not settled(pid, identity)


def wait_gone(pid: int, identity: str, deadline_s: float = 10.0) -> bool:
    """Poll until the recorded incarnation is provably dead.

    :param pid: The recorded pid.
    :param identity: Its captured identity.
    :param deadline_s: Wall-clock budget.
    :returns: ``True`` only on proof of death within the budget.
    """
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        if settled(pid, identity):
            return True
        time.sleep(0.05)
    return settled(pid, identity)


def safe_kill(pid: int, identity: str) -> None:
    """SIGKILL exactly the recorded incarnation (pidfd-pinned on Linux).

    A no-op when the incarnation is already gone or the pid was recycled.

    :param pid: The recorded pid.
    :param identity: Its captured identity.
    """
    _proc.kill_verified(pid, identity, _KILL)
