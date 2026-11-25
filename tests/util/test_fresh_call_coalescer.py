"""Concurrent callers share calls, but never one that began before they asked."""

from __future__ import annotations

import asyncio

import pytest

from omnigent.util.fresh_call_coalescer import FreshCallCoalescer
from tests.budgets import budget


class _GatedCalls:
    """Counts calls; each call waits for its own release and returns its number."""

    def __init__(self) -> None:
        self.started: list[int] = []
        self.releases: list[asyncio.Event] = []

    async def __call__(self) -> int:
        number = len(self.started) + 1
        release = asyncio.Event()
        self.releases.append(release)
        self.started.append(number)
        await asyncio.wait_for(release.wait(), timeout=budget(10))
        return number

    async def wait_started(self, count: int) -> None:
        async def reached() -> None:
            while len(self.started) < count:
                await asyncio.sleep(0)

        await asyncio.wait_for(reached(), timeout=budget(5))


async def test_callers_arriving_mid_call_share_one_following_call() -> None:
    coalescer: FreshCallCoalescer[str, int] = FreshCallCoalescer()
    calls = _GatedCalls()

    first = asyncio.create_task(coalescer.run("session", calls))
    await calls.wait_started(1)
    late = [asyncio.create_task(coalescer.run("session", calls)) for _ in range(5)]
    await asyncio.sleep(0)
    # The late callers wait for a call that starts after they asked.
    assert calls.started == [1]

    calls.releases[0].set()
    assert await first == 1
    await calls.wait_started(2)
    calls.releases[1].set()
    assert await asyncio.gather(*late) == [2] * 5
    assert calls.started == [1, 2]

    # Idle again: the next caller starts a call right away.
    after = asyncio.create_task(coalescer.run("session", calls))
    await calls.wait_started(3)
    calls.releases[2].set()
    assert await after == 3


async def test_keys_run_independently() -> None:
    coalescer: FreshCallCoalescer[str, int] = FreshCallCoalescer()
    calls = _GatedCalls()

    one = asyncio.create_task(coalescer.run("one", calls))
    two = asyncio.create_task(coalescer.run("two", calls))
    await calls.wait_started(2)
    for release in calls.releases:
        release.set()
    assert sorted(await asyncio.gather(one, two)) == [1, 2]


async def test_a_failure_reaches_every_sharer_and_the_next_call_still_runs() -> None:
    coalescer: FreshCallCoalescer[str, int] = FreshCallCoalescer()
    attempts = 0
    blocker = asyncio.Event()

    async def flaky() -> int:
        nonlocal attempts
        attempts += 1
        await asyncio.wait_for(blocker.wait(), timeout=budget(10))
        if attempts <= 2:
            raise RuntimeError(f"git status failed on attempt {attempts}")
        return attempts

    first = asyncio.create_task(coalescer.run("session", flaky))
    while attempts < 1:
        await asyncio.sleep(0)
    sharers = [asyncio.create_task(coalescer.run("session", flaky)) for _ in range(3)]
    await asyncio.sleep(0)
    blocker.set()

    with pytest.raises(RuntimeError, match="attempt 1"):
        await first
    for sharer in sharers:
        with pytest.raises(RuntimeError, match="attempt 2"):
            await sharer
    assert await coalescer.run("session", flaky) == 3


async def test_an_abandoned_caller_leaves_the_shared_call_running() -> None:
    coalescer: FreshCallCoalescer[str, int] = FreshCallCoalescer()
    calls = _GatedCalls()

    first = asyncio.create_task(coalescer.run("session", calls))
    await calls.wait_started(1)
    abandoned = asyncio.create_task(coalescer.run("session", calls))
    kept = asyncio.create_task(coalescer.run("session", calls))
    await asyncio.sleep(0)
    abandoned.cancel()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    calls.releases[0].set()
    await calls.wait_started(2)
    calls.releases[1].set()
    assert await kept == 2
    assert abandoned.cancelled()
