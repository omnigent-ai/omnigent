"""Share an expensive read between concurrent callers without serving stale answers.

A caller that arrives while a call for its key is running cannot reuse that
call's result: the call began before the caller asked, so it may miss a change
the caller is asking about. Such callers instead share the *next* call, which
starts as soon as the running one finishes. Per key, at most one call runs and
at most one more is queued, however many callers pile up.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field
from typing import Generic, TypeVar

_K = TypeVar("_K", bound=Hashable)
_T = TypeVar("_T")


def _mark_retrieved(future: asyncio.Future[object]) -> None:
    """Consume a result every waiter may have abandoned, so asyncio does not log it."""
    if not future.cancelled():
        future.exception()


@dataclass
class _Flight(Generic[_T]):
    running: asyncio.Future[_T]
    queued: asyncio.Future[_T] | None = None
    queued_call: Callable[[], Awaitable[_T]] | None = None
    driver: asyncio.Task[None] | None = field(default=None, repr=False)


class FreshCallCoalescer(Generic[_K, _T]):
    """Run at most one call per key, giving each caller a call that began after it asked.

    Usage::

        scans: FreshCallCoalescer[str, list[str]] = FreshCallCoalescer()
        changes = await scans.run(session_id, lambda: run_slow_io(registry.list, session_id))
    """

    def __init__(self) -> None:
        self._flights: dict[_K, _Flight[_T]] = {}

    async def run(self, key: _K, call: Callable[[], Awaitable[_T]]) -> _T:
        """Return the result of a call for *key* that started no earlier than now.

        :param key: Identifies calls that are interchangeable, e.g.
            ``(session_id, environment_id)``.
        :param call: Starts the read. It runs only if no queued call for *key*
            exists yet; callers that join a queued call share the first one's.
        :returns: The shared call's result. Its exception is raised to every
            caller sharing it. Cancelling this caller leaves the call running
            for the others.
        """
        loop = asyncio.get_running_loop()
        flight = self._flights.get(key)
        if flight is None:
            flight = _Flight(running=loop.create_future())
            flight.running.add_done_callback(_mark_retrieved)
            self._flights[key] = flight
            flight.driver = loop.create_task(self._drive(key, flight, call))
            target = flight.running
        else:
            if flight.queued is None:
                flight.queued = loop.create_future()
                flight.queued.add_done_callback(_mark_retrieved)
                flight.queued_call = call
            target = flight.queued
        return await asyncio.shield(target)

    async def _drive(
        self, key: _K, flight: _Flight[_T], call: Callable[[], Awaitable[_T]]
    ) -> None:
        """Run *call*, then each queued call in turn, until nothing is queued."""
        try:
            while True:
                try:
                    result = await call()
                except Exception as exc:  # noqa: BLE001 — delivered to every waiter
                    flight.running.set_exception(exc)
                else:
                    flight.running.set_result(result)
                if flight.queued is None or flight.queued_call is None:
                    return
                flight.running, call = flight.queued, flight.queued_call
                flight.queued = flight.queued_call = None
        finally:
            for pending in (flight.running, flight.queued):
                if pending is not None and not pending.done():
                    pending.cancel()
            if self._flights.get(key) is flight:
                del self._flights[key]
