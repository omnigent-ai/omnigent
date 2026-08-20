"""Dedicated, bounded thread pool for running git subprocesses.

Both the host (session-start worktree operations, :mod:`omnigent.host.git_worktree`)
and the runner (changed-files / diff view, :mod:`omnigent.runtime.filesystem_registry`)
shell out to ``git``. Those calls block, so they are run off the event loop in a
worker thread — but if they use :func:`asyncio.to_thread` they land on asyncio's
*shared* default ``ThreadPoolExecutor``. That pool also serves the control plane:
the host mints a launch's auth token, polls runners, and stops them through it. A
burst of slow or stuck git commands (a large repo, a cold untracked cache, a
network ``git fetch``) then pins every default-pool worker for the full git
timeout, and every other ``to_thread`` call queues behind them — the event loop
stays alive answering pings while the host stops making progress.

Running git on this separate pool contains that blast radius: a slow git can only
exhaust these workers, never the ones the control plane needs. ``max_workers``
doubles as a cap on concurrent git *processes*, so a stampede of session starts
can't exhaust the box's PIDs/FDs either. Because git is isolated, a generous
subprocess timeout is safe — a hung git delays only other git work, not the host.

Tune the cap with ``OMNIGENT_GIT_MAX_WORKERS`` (positive integer; default 4).
"""

from __future__ import annotations

import asyncio
import functools
import os
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TypeVar

_T = TypeVar("_T")

_DEFAULT_MAX_GIT_WORKERS = 4

_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _max_workers() -> int:
    """Return the git pool size, honoring ``OMNIGENT_GIT_MAX_WORKERS``.

    Read once at pool creation. Falls back to :data:`_DEFAULT_MAX_GIT_WORKERS`
    on unset / invalid / non-positive values.
    """
    raw = os.environ.get("OMNIGENT_GIT_MAX_WORKERS")
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            return value
    return _DEFAULT_MAX_GIT_WORKERS


def git_executor() -> ThreadPoolExecutor:
    """Return the process-wide git thread pool, creating it on first use.

    Lazily built (and cached) so importing this module is cheap and tests can
    set ``OMNIGENT_GIT_MAX_WORKERS`` before the first git call. The pool is
    never explicitly shut down; ``ThreadPoolExecutor``'s atexit hook drains it
    at interpreter exit.

    :returns: The shared bounded executor, threads named ``omni-git-*``.
    """
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                _executor = ThreadPoolExecutor(
                    max_workers=_max_workers(),
                    thread_name_prefix="omni-git",
                )
    return _executor


async def run_git(func: Callable[..., _T], /, *args: object, **kwargs: object) -> _T:
    """Run a blocking, git-invoking callable on the dedicated git pool.

    Drop-in replacement for ``await asyncio.to_thread(func, ...)`` that routes
    to :func:`git_executor` instead of the shared default pool, so a slow git
    can't starve the control plane (see the module docstring).

    :param func: A callable that shells out to git, e.g.
        :func:`omnigent.host.git_worktree.create_worktree`.
    :param args: Positional arguments forwarded to *func*.
    :param kwargs: Keyword arguments forwarded to *func*.
    :returns: Whatever *func* returns.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(git_executor(), functools.partial(func, *args, **kwargs))
