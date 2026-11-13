"""Cross-platform shims for POSIX-only process and identity APIs.

Native Windows lacks ``os.getuid``, ``os.killpg``, ``signal.SIGKILL``
and POSIX sessions. Call sites that only need "the current user",
"hard-kill this process (tree)" or "detach this child" go through these
helpers so modules stay importable, and behave sensibly, on Windows.
"""

from __future__ import annotations

import getpass
import os
import re
import signal
import subprocess
import sys
from typing import Any, Final

IS_WINDOWS: Final[bool] = sys.platform == "win32"

# ``os.kill`` on Windows calls ``TerminateProcess`` for any signal other
# than CTRL_C_EVENT / CTRL_BREAK_EVENT, so SIGTERM is already a hard kill.
SIGKILL: Final[signal.Signals] = getattr(signal, "SIGKILL", signal.SIGTERM)


def current_user_tag() -> str:
    """
    Return a filesystem-safe identifier for the current OS user.

    :returns: The numeric uid on POSIX, e.g. ``"501"``; the sanitized
        login name on Windows, e.g. ``"alice"``.
    """
    getuid = getattr(os, "getuid", None)
    if getuid is not None:
        return str(getuid())
    try:
        name = getpass.getuser()
    except (ImportError, KeyError, OSError):
        name = "user"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name) or "user"


def detached_popen_kwargs() -> dict[str, Any]:
    """
    Return :class:`subprocess.Popen` kwargs that detach a child process.

    POSIX starts a new session so terminal signals (Ctrl-C, SIGHUP) do
    not reach the child. Windows has no sessions; a new process group
    keeps console Ctrl-C events from reaching the child instead.

    :returns: Keyword arguments to splat into ``Popen``.
    """
    if IS_WINDOWS:
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def kill_process_tree(pid: int, sig: int = SIGKILL) -> None:
    """
    Signal *pid*'s process group (POSIX) or process tree (Windows).

    POSIX callers spawn the leader with ``start_new_session=True``, so
    the pid doubles as the process-group id. Windows has no process
    groups to signal, so descendants are enumerated with :mod:`psutil`
    and terminated before the leader.

    :param pid: Leader process id, e.g. ``12345``.
    :param sig: Signal to deliver on POSIX, e.g. ``signal.SIGTERM``.
        Ignored on Windows, where termination is always forceful.
    :raises ProcessLookupError: If *pid* no longer exists.
    """
    killpg = getattr(os, "killpg", None)
    if killpg is not None:
        killpg(pid, sig)
        return
    import psutil

    try:
        leader = psutil.Process(pid)
        children = leader.children(recursive=True)
    except psutil.NoSuchProcess as exc:
        raise ProcessLookupError(pid) from exc
    for child in children:
        try:
            child.kill()
        except psutil.NoSuchProcess:
            continue
    try:
        leader.kill()
    except psutil.NoSuchProcess as exc:
        raise ProcessLookupError(pid) from exc


def pid_exists(pid: int) -> bool:
    """
    Return whether a process with *pid* currently exists.

    POSIX uses the ``os.kill(pid, 0)`` probe. Windows must not: there
    ``os.kill(pid, 0)`` sends ``CTRL_C_EVENT`` to the target's console
    process group instead of probing, so :mod:`psutil` is used.

    :param pid: OS process id, e.g. ``12345``.
    :returns: ``True`` if the process exists (or exists but belongs to
        another user), ``False`` otherwise.
    """
    if pid <= 0:
        return False
    if IS_WINDOWS:
        import psutil

        return psutil.pid_exists(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def tmux_missing_hint() -> str:
    """
    Return extra guidance to append to a "tmux not found" error.

    :returns: A leading-space sentence pointing Windows users at WSL,
        or ``""`` on platforms where tmux can simply be installed.
    """
    if IS_WINDOWS:
        return (
            " tmux is not available on native Windows; run Omnigent inside WSL to use "
            "terminal-based harnesses, or pick an SDK harness (e.g. claude-sdk)."
        )
    return ""
