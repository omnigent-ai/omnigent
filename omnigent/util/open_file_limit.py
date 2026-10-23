"""Raise and inspect the soft open-file limit of Omnigent processes.

macOS launchd starts GUI apps and their children with a soft ``RLIMIT_NOFILE``
of 256 (hard unlimited). A long Omnigent session holds far more descriptors
than that, so the host daemon and the runner raise the soft limit at startup.
"""

from __future__ import annotations

import itertools
import logging
from typing import NamedTuple

import psutil

_logger = logging.getLogger(__name__)

#: Soft limit requested at startup; bounded by the hard limit when that is finite.
DEFAULT_SOFT_OPEN_FILE_LIMIT = 65536
# Darwin's OPEN_MAX; macOS rejects soft limits above ``kern.maxfilesperproc``.
_MACOS_OPEN_MAX = 10240
# Tried in order when the kernel rejects the target; the smaller steps cover
# hosts whose ``kern.maxfilesperproc`` is tuned below OPEN_MAX.
_FALLBACK_SOFT_LIMITS = (_MACOS_OPEN_MAX, 4096, 1024)
#: Share of the limit at which ``omnigent host status`` flags a process.
NEAR_LIMIT_RATIO = 0.8


class OpenFileLimit(NamedTuple):
    """A process's ``RLIMIT_NOFILE`` pair."""

    soft: int
    hard: int


class ProcessOpenFiles(NamedTuple):
    """Open descriptors held by one process.

    :param pid: Process id, e.g. ``4321``.
    :param count: Open descriptor count, e.g. ``251``.
    :param limit: Soft open-file limit, or ``None`` when it cannot be read.
    """

    pid: int
    count: int
    limit: int | None

    @property
    def near_limit(self) -> bool:
        """Whether the process is using at least :data:`NEAR_LIMIT_RATIO` of its limit."""
        return self.limit is not None and self.count >= self.limit * NEAR_LIMIT_RATIO


def _describe(limit: int, infinity: int) -> str:
    return "unlimited" if limit == infinity else str(limit)


def raise_soft_open_file_limit(target: int = DEFAULT_SOFT_OPEN_FILE_LIMIT) -> OpenFileLimit | None:
    """Raise the soft ``RLIMIT_NOFILE`` toward *target*, never above the hard limit.

    No privilege is needed: a process may raise its soft limit up to its hard
    limit. Falls back to macOS's ``OPEN_MAX`` when the kernel rejects *target*.

    :param target: Desired soft limit, e.g. ``65536``.
    :returns: The limits in effect afterwards, or ``None`` on platforms without
        rlimits (Windows).
    """
    try:
        import resource
    except ImportError:
        return None
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    infinity = resource.RLIM_INFINITY
    ceiling = target if hard == infinity else min(hard, target)
    if soft == infinity or soft >= ceiling:
        return OpenFileLimit(soft, hard)
    failures: list[str] = []
    for wanted in (ceiling, *(limit for limit in _FALLBACK_SOFT_LIMITS if limit < ceiling)):
        if wanted <= soft:
            continue
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (wanted, hard))
        except (OSError, ValueError) as exc:
            failures.append(f"{wanted}: {exc}")
            continue
        _logger.info(
            "raised soft open-file limit from %d to %d (hard %s)",
            soft,
            wanted,
            _describe(hard, infinity),
        )
        return OpenFileLimit(*resource.getrlimit(resource.RLIMIT_NOFILE))
    # An inherited limit already at or above OPEN_MAX leaves ample headroom.
    log = _logger.info if soft >= _MACOS_OPEN_MAX else _logger.warning
    log(
        "could not raise soft open-file limit from %d (hard %s): %s; long sessions "
        "may run out of file descriptors",
        soft,
        _describe(hard, infinity),
        "; ".join(failures),
    )
    return OpenFileLimit(soft, hard)


# Host children are launched as ``python -m omnigent...``; other entry points are not counted.
def _runs_omnigent_module(cmdline: list[str]) -> bool:
    return any(
        flag == "-m" and module.split(".")[0] == "omnigent"
        for flag, module in itertools.pairwise(cmdline)
    )


def _soft_limit(process: psutil.Process, inherited_limit: int | None) -> int | None:
    # psutil reads another process's rlimit only on Linux and FreeBSD.
    try:
        soft = (
            process.rlimit(psutil.RLIMIT_NOFILE)[0]
            if hasattr(process, "rlimit")
            else inherited_limit
        )
    except psutil.AccessDenied:
        soft = inherited_limit
    return soft if soft is not None and soft > 0 else None


def host_process_open_files(
    root_pid: int, *, inherited_limit: int | None
) -> list[ProcessOpenFiles]:
    """Count open descriptors for a host daemon and its Omnigent child processes.

    The zygote, runners and a local server inherit the daemon's raised limit.

    :param root_pid: Host daemon pid, e.g. ``4242``.
    :param inherited_limit: Daemon's recorded soft limit, used where psutil
        cannot read another process's limit (macOS).
    :returns: Readable processes, the one closest to its limit first; empty
        where descriptor counts are unavailable (Windows).
    """
    if not hasattr(psutil.Process, "num_fds"):
        return []
    try:
        root = psutil.Process(root_pid)
        processes = [root, *root.children(recursive=True)]
    except psutil.Error:
        return []
    usage: list[ProcessOpenFiles] = []
    for process in processes:
        try:
            if process.pid != root_pid and not _runs_omnigent_module(process.cmdline()):
                continue
            usage.append(
                ProcessOpenFiles(
                    pid=process.pid,
                    count=process.num_fds(),
                    limit=_soft_limit(process, inherited_limit),
                )
            )
        except psutil.Error:
            continue
    return sorted(
        usage,
        key=lambda entry: (entry.count / entry.limit if entry.limit else 0.0, entry.count),
        reverse=True,
    )
