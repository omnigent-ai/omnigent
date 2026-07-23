"""Cross-platform child-process spawning and tree teardown.

Historically omnigent terminated child agent processes by killing their POSIX
process group (``os.killpg``), which only works because children are spawned
with ``start_new_session=True`` so ``pid == pgid``. Neither process groups nor
``os.killpg`` exist on Windows, so this module centralizes the portable
equivalents:

* :func:`spawn_kwargs` — the ``Popen``/``create_subprocess_exec`` keyword args
  that put a child in its own group/session (so signals don't leak to the
  parent and the whole tree can be torn down).
* :func:`terminate_tree` / :func:`kill_tree` — recursively stop a process and
  all of its descendants, using the process-group fast path on POSIX and
  :mod:`psutil` walking on every platform.
* :func:`process_alive` — liveness check that doesn't rely on ``os.kill(pid, 0)``.

:mod:`psutil` is already a core dependency, so the descendant walk needs no new
package.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
from contextlib import suppress
from typing import Protocol

import psutil

from omnigent._platform import IS_POSIX

logger = logging.getLogger(__name__)

# Resolved via getattr so this module type-checks and imports on Windows, where
# process groups and SIGKILL do not exist. None on non-POSIX hosts.
_killpg_fn = getattr(os, "killpg", None)
_getpgid_fn = getattr(os, "getpgid", None)
_SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)


class _ProcessLike(Protocol):
    """The subset of ``subprocess.Popen`` / ``asyncio.subprocess.Process`` used here."""

    @property
    def pid(self) -> int | None:
        pass

    @property
    def returncode(self) -> int | None:
        pass

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass


def spawn_kwargs() -> dict[str, object]:
    """
    Keyword args that isolate a child process into its own group/session.

    On POSIX returns ``{"start_new_session": True}`` (new session, so the child
    becomes a process-group leader and ``os.killpg(pid, ...)`` reaps the whole
    tree). On Windows returns ``{"creationflags": CREATE_NEW_PROCESS_GROUP}``
    so the child is in its own Ctrl-C group and can be torn down independently
    of the parent console.

    Pass via ``**spawn_kwargs()`` to :class:`subprocess.Popen` or
    :func:`asyncio.create_subprocess_exec`.
    """
    if IS_POSIX:
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}


def _killpg(pid: int, sig: int) -> bool:
    """
    POSIX fast path: signal the child's whole process group.

    Refuses to signal our OWN process group. A child spawned without
    ``start_new_session`` never becomes a group leader, so ``getpgid(pid)``
    resolves to the group we *share* with our parent — pytest, the
    harness/runner supervisor, the CI job step. ``killpg`` on that group would
    take down this process and everything around it (observed in CI as a
    job-wide "runner received shutdown signal" cancelling e2e at ~96%). The old
    code passed ``pid`` itself as the pgid, which failed safe for a non-leader
    (no group is numbered ``pid`` → ``ProcessLookupError`` → caller falls back);
    resolving the real group removed that accidental safety. Returning False
    here makes :func:`terminate_tree` / :func:`kill_tree` fall back to the
    psutil per-descendant walk, which signals only the real target subtree.

    :returns: True if the group signal was delivered, False if process groups
        are unavailable (Windows), the lookup failed, or the target group is
        our own.
    """
    if not IS_POSIX or _killpg_fn is None or _getpgid_fn is None:
        return False
    try:
        target_pgid = _getpgid_fn(pid)
        if target_pgid == _getpgid_fn(0):
            return False
        _killpg_fn(target_pgid, sig)
        return True
    except (ProcessLookupError, PermissionError, OSError):
        return False


def _walk_descendants(pid: int) -> list[psutil.Process]:
    """Return the process plus all live descendants, innermost-last is not guaranteed."""
    try:
        root = psutil.Process(pid)
    except psutil.NoSuchProcess:
        return []
    procs = [root]
    with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        procs.extend(root.children(recursive=True))
    return procs


def terminate_tree(process: _ProcessLike | None, *, grace: float = 0.0) -> None:
    """
    Gracefully stop ``process`` and all of its descendants.

    Sends ``SIGTERM`` (POSIX) / ``terminate()`` (Windows ``TerminateProcess``)
    to the whole tree. On POSIX the process-group fast path is tried first;
    otherwise (and on Windows) the tree is walked with :mod:`psutil`. Already
    exited processes are no-ops. All "process gone / not permitted" errors are
    swallowed — teardown is best-effort.

    :param process: A ``Popen``/``asyncio`` process handle, or ``None``.
    :param grace: Optional seconds to wait for the tree to exit after signaling.
    """
    if process is None or process.returncode is not None:
        return
    pid = process.pid
    if pid is None:
        with suppress(Exception):
            process.terminate()
        return

    if _killpg(pid, signal.SIGTERM):
        if grace:
            _wait_gone(pid, grace)
        return

    procs = _walk_descendants(pid)
    for proc in procs:
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.terminate()
    if not procs:
        with suppress(Exception):
            process.terminate()
    if grace:
        _wait_gone(pid, grace)


def kill_tree(process: _ProcessLike | None) -> None:
    """
    Forcibly kill ``process`` and all of its descendants.

    Like :func:`terminate_tree` but with ``SIGKILL`` (POSIX) /
    ``TerminateProcess`` (Windows). Use after a grace period when a graceful
    terminate did not take.
    """
    if process is None or process.returncode is not None:
        return
    pid = process.pid
    if pid is None:
        with suppress(Exception):
            process.kill()
        return

    if _killpg(pid, _SIGKILL):
        return

    procs = _walk_descendants(pid)
    for proc in procs:
        with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
            proc.kill()
    if not procs:
        with suppress(Exception):
            process.kill()


def _wait_gone(pid: int, timeout: float) -> None:
    with suppress(psutil.NoSuchProcess, psutil.AccessDenied):
        psutil.Process(pid).wait(timeout=timeout)


def process_start_identity(pid: int) -> str | None:
    """
    Stable identity for ``pid``'s current incarnation, or ``None`` if gone.

    Uses the kernel-reported process start time (sub-second float), which a
    recycled pid can never reproduce, so ``identity == recorded`` proves the
    pid still names the same process. An unreadable process (gone, zombie on
    platforms that hide it, foreign) yields ``None``, which never matches —
    conservative in the don't-kill-strangers direction.

    :param pid: The process id to identify.
    :returns: An opaque identity string, or ``None``.
    """
    if pid <= 0:
        return None
    try:
        return repr(psutil.Process(pid).create_time())
    except (psutil.Error, OSError):
        return None


def process_identity_state(pid: int, identity: str | None) -> str:
    """
    Classify *pid* against a recorded :func:`process_start_identity`.

    :param pid: The process id to probe.
    :param identity: The recorded identity to compare against.
    :returns: ``"match"`` — same incarnation, safe to signal;
        ``"gone"`` — definitively absent, or the pid now belongs to a
        different incarnation; ``"unverifiable"`` — something exists but
        its identity cannot be read (treat as possibly ours and alive).
    """
    if pid <= 0 or identity is None:
        return "gone"
    try:
        current = repr(psutil.Process(pid).create_time())
    except psutil.NoSuchProcess:
        # Includes psutil.ZombieProcess on platforms that hide zombie
        # metadata — a zombie holds no resources worth waiting for.
        return "gone"
    except (psutil.Error, OSError):
        return "unverifiable"
    return "match" if current == identity else "gone"


def group_member_identities(pgid: int) -> dict[int, str] | None:
    """
    Snapshot ``pid -> identity`` for every member of process group *pgid*.

    Only meaningful while group ownership is provable (e.g. a verified live
    leader); the caller records the result and later kills exactly these
    identities via :func:`kill_verified`. The snapshot fails closed: a pid
    whose membership or identity cannot be read (rather than being
    definitively gone) makes the whole snapshot ``None``, because a
    silently incomplete kill list would leak the unreadable member.

    :param pgid: The process group to enumerate.
    :returns: Member identities, or ``None`` when a complete snapshot
        could not be taken.
    """
    if pgid <= 0 or _getpgid_fn is None:
        return None
    try:
        pids = psutil.pids()
    except (psutil.Error, OSError):
        return None
    members: dict[int, str] = {}
    for pid in pids:
        # Membership → identity → membership: non-members (gone, foreign,
        # unreadable) are skipped without touching their identity, so a
        # stranger can never poison the snapshot; the re-check after the
        # identity read proves the identity belongs to the incarnation
        # that is (still) in the group, so a pid recycled mid-scan cannot
        # bind an unrelated process to the kill list.
        try:
            if _getpgid_fn(pid) != pgid:
                continue
        except OSError:
            continue  # gone or not ours to inspect — not a member of ours
        try:
            identity = repr(psutil.Process(pid).create_time())
        except psutil.NoSuchProcess:
            continue  # exited between the group check and the identity read
        except (psutil.Error, OSError):
            return None  # a confirmed member is unreadable — fail closed
        try:
            if _getpgid_fn(pid) != pgid:
                continue
        except OSError:
            continue
        members[pid] = identity
    return members or None


def process_is_zombie(pid: int) -> bool:
    """
    Whether *pid* is an unreaped zombie — dead for reaping purposes.

    A zombie's metadata stays readable on Linux, so identity probes report
    ``"match"``; callers deciding "is there anything left to wait for"
    must pair the identity check with this one. A zombie holds no
    sockets, memory, or CPU — only an exit status awaiting collection.

    :param pid: The pid to probe.
    :returns: ``True`` for an unreaped zombie.
    """
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except (psutil.Error, OSError):
        return False


def group_has_live_members(pgid: int) -> bool | None:
    """
    Whether process group *pgid* has any live (non-zombie) members.

    Single /proc pass. Zombies are ignored (their collection belongs to a
    foreign reaper). Fail-closed: an unenumerable pid table or an
    unreadable confirmed member yields ``None``.

    IMPORTANT — soundness scope. This scan CANNOT prove a group empty on
    its own: a member can fork a survivor after the pid listing and exit,
    hiding the survivor. It is therefore safe ONLY where the caller is
    actively ``SIGKILL``-ing the whole group before it scans — a process
    with a pending ``SIGKILL`` cannot return to userspace to fork again,
    so no new generation can appear after the kill and the scan can only
    err toward "still live" (retain), never toward a false "empty". For a
    release decision WITHOUT such a kill (the fallback tiers, which signal
    only recorded members), use :func:`group_kernel_present` instead — the
    kernel's own group check, which no fork relay can outrun.

    :param pgid: The process group to probe.
    :returns: ``True`` when a live member exists, ``False`` for a complete
        scan that found none, ``None`` when indeterminate.
    """
    if pgid <= 0 or _getpgid_fn is None:
        return None
    try:
        pids = psutil.pids()
    except (psutil.Error, OSError):
        return None
    for pid in pids:
        try:
            if _getpgid_fn(pid) != pgid:
                continue
        except ProcessLookupError:
            continue  # exited mid-scan — definitively not a member anymore
        except OSError:
            return None  # membership unknowable — scan incomplete
        try:
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                continue
        except psutil.NoSuchProcess:
            continue
        except (psutil.Error, OSError):
            return None  # a confirmed member is unreadable — inconclusive
        return True
    return False


def group_kernel_present(pgid: int) -> bool | None:
    """
    Whether the kernel reports process group *pgid* as having any members.

    ``killpg(pgid, 0)`` is atomic and authoritative: the kernel checks the
    whole group, so no fork-relay racing a userspace scan can hide from
    it. ``ESRCH`` means the group is provably empty of ALL members (live
    and zombie) — the only sound basis for deleting a tier's evidence when
    that tier cannot itself kill unrecorded group members. A zombie keeps
    the group present until a foreign reaper collects it, so release lags
    that collection (bounded, and it is only on-disk evidence).

    :param pgid: The process group to probe.
    :returns: ``False`` when provably empty (``ESRCH``), ``True`` when a
        member exists (including a foreign-owned ``EPERM`` one), ``None``
        when indeterminate.
    """
    if pgid <= 0 or _killpg_fn is None:
        return None
    try:
        _killpg_fn(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def kill_verified(pid: int, identity: str, sig: int) -> bool:
    """
    Deliver *sig* to *pid* only if it is still the recorded incarnation.

    On Linux the target is pinned with a pidfd before the identity
    re-check, so the signal provably reaches the verified process and pid
    reuse cannot be raced. Other platforms (macOS has no equivalent
    user-space handle) re-check the identity immediately before a plain
    kill — a residual reuse window of microseconds remains there.

    :param pid: The process id to signal.
    :param identity: Its recorded :func:`process_start_identity`.
    :param sig: The signal to deliver.
    :returns: ``True`` if the signal was delivered to the verified target.
    """
    if process_identity_state(pid, identity) != "match":
        return False
    pidfd_open = getattr(os, "pidfd_open", None)
    pidfd_send = getattr(signal, "pidfd_send_signal", None)
    if pidfd_open is not None and pidfd_send is not None:
        try:
            fd = pidfd_open(pid)
        except OSError:
            return False
        try:
            if process_identity_state(pid, identity) != "match":
                return False
            pidfd_send(fd, sig)
            return True
        except OSError:
            return False
        finally:
            with suppress(OSError):
                os.close(fd)
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False


def process_alive(pid: int) -> bool:
    """
    Whether ``pid`` names a live, non-zombie process.

    Cross-platform replacement for the ``os.kill(pid, 0)`` liveness probe (which
    behaves differently on Windows). A zombie/defunct process counts as not
    alive — it has exited and is only awaiting reaping.

    :param pid: The process id to probe.
    :returns: True if the process exists and has not exited.
    """
    if pid <= 0:
        return False
    try:
        return psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        # Includes psutil.ZombieProcess (a NoSuchProcess subclass).
        return False
    except psutil.AccessDenied:
        # Exists but belongs to another user / can't introspect -> alive.
        return True
