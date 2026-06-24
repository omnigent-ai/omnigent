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

    :returns: True if the group signal was delivered, False if process groups
        are unavailable (Windows) or the lookup failed.
    """
    if not IS_POSIX or _killpg_fn is None or _getpgid_fn is None:
        return False
    try:
        _killpg_fn(_getpgid_fn(pid), sig)
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
