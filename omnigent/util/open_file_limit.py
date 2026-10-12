"""Raise this process's soft open-file limit toward its hard limit.

macOS launchd starts GUI apps and their children with a soft ``RLIMIT_NOFILE``
of 256 (hard unlimited). A long Omnigent session holds far more descriptors
than that, so the host daemon and the runner raise the soft limit at startup.
"""

from __future__ import annotations

import logging
from typing import NamedTuple

_logger = logging.getLogger(__name__)

#: Soft limit requested at startup; bounded by the hard limit when that is finite.
DEFAULT_SOFT_OPEN_FILE_LIMIT = 65536
# macOS OPEN_MAX: the largest soft limit the kernel accepts when the hard limit is
# unlimited but ``kern.maxfilesperproc`` is below the requested value.
_MACOS_OPEN_MAX = 10240
# Tried in order when the kernel rejects the target; the smaller steps cover
# hosts whose ``kern.maxfilesperproc`` is tuned below OPEN_MAX.
_FALLBACK_SOFT_LIMITS = (_MACOS_OPEN_MAX, 4096, 1024)


class OpenFileLimit(NamedTuple):
    """A process's ``RLIMIT_NOFILE`` pair."""

    soft: int
    hard: int


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
    candidates = [ceiling, *(limit for limit in _FALLBACK_SOFT_LIMITS if limit < ceiling)]
    failures: list[str] = []
    for wanted in candidates:
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
