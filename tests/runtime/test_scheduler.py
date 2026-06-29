"""Unit tests for the SchedulerService engine (B1).

The clock, sleep, fire callback, and store are all injected, so these tests
run deterministically with no real waiting and no host coupling.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

from omnigent.entities.schedule import Schedule
from omnigent.runtime.scheduler import SchedulerService

_FIXED = datetime(2026, 1, 2, 3, 4, 30, tzinfo=timezone.utc)


def _schedule(
    schedule_id: str,
    *,
    kind: str = "loop",
    cron: str | None = "* * * * *",
) -> Schedule:
    return Schedule(
        id=schedule_id,
        conversation_id="conv_1",
        name=schedule_id,
        kind=kind,
        prompt="do the thing",
        enabled=True,
        status="idle",
        created_at=0,
        cron=cron,
    )


class _FakeStore:
    """Minimal ScheduleStore stand-in: preset enabled rows + update recorder."""

    def __init__(self, schedules: list[Schedule]) -> None:
        self._schedules = schedules
        self.updates: list[tuple[str, dict[str, Any]]] = []

    def list_enabled(self) -> list[Schedule]:
        return list(self._schedules)

    def update(self, schedule_id: str, **kwargs: Any) -> None:
        self.updates.append((schedule_id, kwargs))


def _fire_recorder() -> tuple[Callable[[Schedule], Awaitable[None]], list[str], asyncio.Event]:
    fired = asyncio.Event()
    calls: list[str] = []

    async def fire(s: Schedule) -> None:
        calls.append(s.id)
        fired.set()

    return fire, calls, fired


def _fire_once_then_park() -> Callable[[float], Awaitable[None]]:
    """A fake sleep that returns immediately the first time, then parks.

    Lets ``_run_loop`` fire exactly once and then block on its second
    sleep, so a test can assert the single fire without the loop spinning.
    """
    count = 0

    async def sleep(_secs: float) -> None:
        nonlocal count
        count += 1
        if count >= 2:
            await asyncio.Event().wait()

    return sleep


async def test_fires_loop_and_records_last_fired() -> None:
    store = _FakeStore([_schedule("s1", cron="* * * * *")])
    fire, calls, fired = _fire_recorder()

    svc = SchedulerService(store, fire, now=lambda: _FIXED, sleep=_fire_once_then_park())
    await svc.start()
    try:
        await asyncio.wait_for(fired.wait(), timeout=2)
    finally:
        await svc.stop()

    assert calls == ["s1"]
    assert store.updates[0][0] == "s1"
    assert store.updates[0][1]["last_fired_at"] == int(_FIXED.timestamp())


async def test_skips_monitors_and_cronless_loops() -> None:
    store = _FakeStore(
        [
            _schedule("m1", kind="monitor", cron=None),
            _schedule("l0", kind="loop", cron=None),
        ]
    )
    fire, calls, _ = _fire_recorder()

    svc = SchedulerService(store, fire, now=lambda: _FIXED, sleep=_fire_once_then_park())
    await svc.start()
    try:
        await asyncio.sleep(0.02)
        assert calls == []
        assert svc._tasks == {}  # nothing armed
    finally:
        await svc.stop()


async def test_invalid_cron_is_skipped_without_firing() -> None:
    store = _FakeStore([_schedule("bad", cron="not a cron")])
    fire, calls, _ = _fire_recorder()

    svc = SchedulerService(store, fire, now=lambda: _FIXED)
    await svc.start()
    try:
        await asyncio.sleep(0.02)  # let the task run + return early
        assert calls == []
    finally:
        await svc.stop()


async def test_refresh_cancels_removed_schedule() -> None:
    store = _FakeStore([_schedule("s1", cron="* * * * *")])
    fire, _, _ = _fire_recorder()

    svc = SchedulerService(store, fire, now=lambda: _FIXED, sleep=_fire_once_then_park())
    await svc.start()
    try:
        assert set(svc._tasks) == {"s1"}
        store._schedules = []  # s1 disabled/removed
        await svc.refresh()
        assert svc._tasks == {}
    finally:
        await svc.stop()


def test_seconds_until_next_is_within_a_minute() -> None:
    svc = SchedulerService(_FakeStore([]), _fire_recorder()[0])
    secs = svc._seconds_until_next("* * * * *")
    assert 0 <= secs <= 60
