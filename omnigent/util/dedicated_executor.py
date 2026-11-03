"""A lazily started thread pool kept apart from the event loop's default executor.

Work offloaded with ``asyncio.to_thread`` shares one default pool, so a backlog
of slow jobs there delays every other offload. Giving a category of work its own
``DedicatedExecutor`` keeps it from competing with that pool. Worker threads
start on first use, so a process that forks before using one has none to copy.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import ParamSpec, TypeVar

from omnigent.util.thread_pool_watchdog import watch_thread_pool

_P = ParamSpec("_P")
_T = TypeVar("_T")


class DedicatedExecutor:
    """Run blocking calls on a separate, lazily created ``ThreadPoolExecutor``.

    :param max_workers: Worker limit, e.g. ``4``.
    :param thread_name_prefix: Worker thread name prefix, e.g. ``"runner-slow-io"``.
    """

    def __init__(self, *, max_workers: int, thread_name_prefix: str) -> None:
        self._max_workers = max_workers
        self._thread_name_prefix = thread_name_prefix
        self._executor: ThreadPoolExecutor | None = None
        self._lock = threading.Lock()

    def _get_executor(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._executor is None:
                self._executor = ThreadPoolExecutor(
                    max_workers=self._max_workers,
                    thread_name_prefix=self._thread_name_prefix,
                )
                watch_thread_pool(self._executor, thread_name_prefix=self._thread_name_prefix)
            return self._executor

    async def run(self, func: Callable[_P, _T], /, *args: _P.args, **kwargs: _P.kwargs) -> _T:
        """Run *func* on this pool, like :func:`asyncio.to_thread`.

        :param func: Blocking callable, e.g. ``registry.list_changed_files``.
        :param args: Positional arguments for *func*.
        :param kwargs: Keyword arguments for *func*.
        :returns: The value *func* returns.
        """
        loop = asyncio.get_running_loop()
        call: Callable[[], _T] = functools.partial(
            contextvars.copy_context().run, func, *args, **kwargs
        )
        return await loop.run_in_executor(self._get_executor(), call)
