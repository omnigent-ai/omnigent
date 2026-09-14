"""Track terminal child processes when their owner also reaps orphaned children."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable, Iterator
from contextvars import ContextVar
from typing import Any

_current_owner: ContextVar[SubprocessOwnership | None] = ContextVar(
    "terminal_subprocess_owner", default=None
)


class SubprocessOwnership:
    """Keep asyncio-owned child statuses out of a daemon's orphan reaper."""

    def __init__(self) -> None:
        self.spawning = 0
        self.pids: set[int] = set()
        self._waiters: set[asyncio.Task[int]] = set()

    @contextlib.contextmanager
    def scope(self) -> Iterator[None]:
        token = _current_owner.set(self)
        try:
            yield
        finally:
            _current_owner.reset(token)

    def track(self, process: asyncio.subprocess.Process) -> None:
        self.pids.add(process.pid)
        waiter = asyncio.create_task(process.wait(), name="terminal-child-status")
        self._waiters.add(waiter)

        def finished(task: asyncio.Task[int]) -> None:
            self.pids.discard(process.pid)
            self._waiters.discard(task)
            if not task.cancelled():
                task.exception()

        waiter.add_done_callback(finished)


async def create_subprocess_exec(
    spawn: Callable[..., Awaitable[asyncio.subprocess.Process]],
    *args: Any,
    **kwargs: Any,
) -> asyncio.subprocess.Process:
    """Spawn normally, registering status ownership only inside an owner's scope."""
    owner = _current_owner.get()
    if owner is None:
        return await spawn(*args, **kwargs)
    owner.spawning += 1
    try:
        process = await spawn(*args, **kwargs)
        owner.track(process)
        return process
    finally:
        owner.spawning -= 1
