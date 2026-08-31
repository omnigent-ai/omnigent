"""Reap Omnigent processes leaked by a test session.

Tests spawn real Omnigent subprocesses — detached host daemons
(``omnigent.host._daemon_entry``), local servers (``omnigent.cli server``),
and runner zygotes (``omnigent.runner._zygote``). They are started with
``start_new_session=True``, so nothing reaps them when pytest exits:
survivors squat the local server's preferred port (6767, displacing a
developer's real server onto a random port) and keep serving after their
temp data dir is deleted (``/health`` answers 200 over an unlinked SQLite
file).

The session fixture points ``OMNIGENT_DATA_DIR`` at a throwaway
per-session directory, and every spawn path propagates that variable to
its children (the host-daemon and runner env allowlists both carry it), so
the directory doubles as an attribution key: a live Omnigent process whose
exec-time environment or command line references it was spawned by this
session and is safe to terminate. Nothing else on the machine — another
test session, a developer's real server — ever references this exact path.

Entry point: :func:`reap_leaked_omnigent_processes`.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

import psutil

# Grace between SIGTERM and SIGKILL: long enough for a daemon's clean
# shutdown path (which also tears down its zygote children), short enough
# to keep session teardown snappy.
_REAP_TIMEOUT_S = 10.0
# Post-SIGKILL wait: KILL is not ignorable, so survivors vanish fast; a
# short bound keeps total teardown near the TERM grace, not double it.
_KILL_WAIT_S = 5.0


def _proc_argv(proc: psutil.Process) -> list[str]:
    """Return a process's argv, or ``[]`` when unreadable.

    :param proc: The process to inspect.
    :returns: Its argv list; empty for zombies/foreign processes.
    """
    try:
        return proc.cmdline()
    except (psutil.Error, OSError):
        return []


def _proc_environ(proc: psutil.Process) -> dict[str, str]:
    """Return a process's exec-time environment, or ``{}`` when unreadable.

    :param proc: The process to inspect.
    :returns: Its environment dict; empty for zombies/foreign processes.
    """
    try:
        return proc.environ()
    except (psutil.Error, OSError):
        return {}


def _cmdline_references_dir(argv: list[str], needle: str) -> bool:
    """Whether *argv* references the directory *needle* with path boundaries.

    A raw substring test would let one session's dir prefix-match a
    sibling's (``/tmp/omnigent-pytest-a`` inside
    ``/tmp/omnigent-pytest-abcd/chat.db``), attributing — and killing —
    another session's process. Match only when a token is exactly the
    directory, ends with it (``--flag=<dir>``), or contains it followed by
    a path separator (``sqlite:///<dir>/chat.db``).

    :param argv: The process's argv.
    :param needle: The data-dir path, already stripped of a trailing sep.
    :returns: ``True`` when the command line references the directory.
    """
    bounded = needle + os.sep
    return any(arg == needle or arg.endswith(needle) or bounded in arg for arg in argv)


def _is_pytest_process(argv: list[str]) -> bool:
    """Whether *argv* is a pytest run rather than a spawned Omnigent process.

    Token-based on purpose: a leaked server's ``--database-uri`` embeds the
    ``omnigent-pytest-<rand>`` data-dir name, so a substring test on the
    joined command line would mistake the very orphans this module reaps
    for pytest runs.

    :param argv: The process's argv.
    :returns: ``True`` for ``pytest`` / ``python -m pytest`` invocations.
    """
    return any(arg == "pytest" or os.path.basename(arg) == "pytest" for arg in argv)


def find_leaked_omnigent_processes(data_dir: Path | str) -> list[psutil.Process]:
    """Find live Omnigent processes attributable to *data_dir*.

    A process is attributed when its exec-time ``OMNIGENT_DATA_DIR`` equals
    *data_dir* (host daemons and zygotes inherit it via the spawn-env
    allowlists) or its command line references the directory (a spawned
    server's ``--database-uri sqlite:///<data_dir>/chat.db``). pytest
    processes are never matched: a concurrent pytest (a nested run, an
    xdist worker exec'd with this process's pre-conftest environment) can
    carry this session's ``OMNIGENT_DATA_DIR`` at exec time without being
    a leak.

    :param data_dir: The session's throwaway ``OMNIGENT_DATA_DIR``.
    :returns: Matching processes, excluding the caller.
    """
    needle = str(data_dir).rstrip(os.sep)
    # A blank or root-ish needle would match everything; refuse it.
    if not needle or needle == os.sep:
        return []
    me = os.getpid()
    leaked: list[psutil.Process] = []
    for proc in psutil.process_iter():
        if proc.pid == me:
            continue
        argv = _proc_argv(proc)
        cmdline = " ".join(argv)
        if "omnigent" not in cmdline or _is_pytest_process(argv):
            continue
        env = _proc_environ(proc)
        if env.get("OMNIGENT_DATA_DIR", "").rstrip(os.sep) != needle and not (
            _cmdline_references_dir(argv, needle)
        ):
            continue
        leaked.append(proc)
    return leaked


def reap_leaked_omnigent_processes(
    data_dir: Path | str, *, timeout: float = _REAP_TIMEOUT_S
) -> tuple[list[str], list[str]]:
    """Terminate every Omnigent process attributable to *data_dir*.

    TERM first (a daemon's clean shutdown also stops the children it
    owns), then KILL whatever remains after *timeout*.

    :param data_dir: The session's throwaway ``OMNIGENT_DATA_DIR``.
    :param timeout: Seconds to wait between TERM and KILL.
    :returns: ``(reaped, survivors)`` command lines: processes confirmed
        gone, and any that outlived even SIGKILL (e.g. stuck in
        uninterruptible sleep) so a genuine non-reap stays visible.
    """
    leaked = find_leaked_omnigent_processes(data_dir)
    if not leaked:
        return [], []
    cmdline_by_pid = {proc.pid: " ".join(_proc_argv(proc)) for proc in leaked}
    for proc in leaked:
        with contextlib.suppress(psutil.Error, OSError):
            proc.terminate()
    _, alive = psutil.wait_procs(leaked, timeout=timeout)
    for proc in alive:
        with contextlib.suppress(psutil.Error, OSError):
            proc.kill()
    _, survivors = psutil.wait_procs(alive, timeout=_KILL_WAIT_S)
    survivor_pids = {proc.pid for proc in survivors}
    reaped = [cmd for pid, cmd in cmdline_by_pid.items() if pid not in survivor_pids]
    return reaped, [cmdline_by_pid[pid] for pid in survivor_pids]
