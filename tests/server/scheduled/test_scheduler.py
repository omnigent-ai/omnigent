"""Tests for :class:`ScheduledTaskScheduler`.

Exercises boot-load from ``list_active_all_workspaces()``, per-task arming, the
injected ``on_fire`` seam, SKIP overlap policy, the misfire grace window, and the
``add``/``update``/``remove`` CRUD sync methods.

Timing is fully controllable: a fake clock supplies "now", and a fake timer
records armed delays and lets the test fire them manually — no wall-clock
sleeps.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace

import pytest

from omnigent.server.scheduled.scheduler import (
    MISFIRE_GRACE_TIME_S,
    ScheduledTaskScheduler,
)


@dataclass
class _FakeTask:
    """The slice of ``ScheduledTask`` the scheduler reads.

    The scheduler only touches ``id``, ``workspace_id``, ``rrule``,
    ``timezone``, and ``state``, so the tests drive it with this local stand-in
    rather than the full persisted entity — keeping the scheduler unit tests
    independent of the entity's field set.
    """

    id: str
    rrule: str
    timezone: str
    state: str
    workspace_id: int = 0


# ── Fakes ────────────────────────────────────────────────────────────────────


class FakeClock:
    """A monotonic-ish wall clock the test advances by hand.

    ``now()`` returns epoch seconds (float); the scheduler reads it for both
    scheduling math (via its tz-aware datetime) and misfire checks.
    """

    def __init__(self, start: float = 1_800_000_000.0) -> None:
        self._t = start

    def now(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


@dataclass
class FakeTimer:
    """One armed callback recorded by :class:`FakeScheduleSeam`."""

    delay: float
    callback: object
    cancelled: bool = False


class FakeScheduleSeam:
    """Stands in for ``loop.call_later``. Records armed timers so the test can
    inspect delays and fire them deterministically."""

    def __init__(self) -> None:
        self.timers: list[FakeTimer] = []

    def __call__(self, delay: float, callback: object) -> FakeTimer:
        timer = FakeTimer(delay=delay, callback=callback)
        self.timers.append(timer)
        return timer

    @staticmethod
    def cancel(timer: FakeTimer) -> None:
        timer.cancelled = True

    def live(self) -> list[FakeTimer]:
        return [t for t in self.timers if not t.cancelled]

    async def fire_latest(self) -> None:
        """Invoke the most-recently armed live timer's callback."""
        timer = self.live()[-1]
        timer.cancelled = True
        result = timer.callback()  # type: ignore[operator]
        if asyncio.iscoroutine(result):
            await result


class FakeStore:
    """Minimal stand-in exposing only scheduler store methods."""

    def __init__(self, tasks: list[_FakeTask]) -> None:
        self._tasks = {t.id: t for t in tasks}

    def list_active_all_workspaces(self) -> list[_FakeTask]:
        return [t for t in self._tasks.values() if t.state == "active"]

    def get(self, task_id: str):
        return self._tasks.get(task_id)


def _task(
    task_id: str = "task-1",
    rrule: str = "FREQ=HOURLY",
    timezone: str = "UTC",
    state: str = "active",
    workspace_id: int = 0,
) -> _FakeTask:
    return _FakeTask(
        id=task_id,
        rrule=rrule,
        timezone=timezone,
        state=state,
        workspace_id=workspace_id,
    )


@dataclass
class FiredRecord:
    """Records calls to the injected on_fire callback."""

    calls: list[tuple[int, str]] = field(default_factory=list)

    async def on_fire(self, workspace_id: int, scheduled_task_id: str) -> None:
        self.calls.append((workspace_id, scheduled_task_id))


def _make(
    tasks: list[_FakeTask],
    fired: FiredRecord | None = None,
    clock: FakeClock | None = None,
    seam: FakeScheduleSeam | None = None,
    on_fire=None,
) -> tuple[ScheduledTaskScheduler, FakeClock, FakeScheduleSeam, FiredRecord]:
    fired = fired or FiredRecord()
    clock = clock or FakeClock()
    seam = seam or FakeScheduleSeam()
    scheduler = ScheduledTaskScheduler(
        store=FakeStore(tasks),
        on_fire=on_fire or fired.on_fire,
        now=clock.now,
        schedule_call=seam,
        cancel_call=seam.cancel,
    )
    return scheduler, clock, seam, fired


# ── boot load ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rule", ["FREQ=HOURLY", "FREQ=MINUTELY;INTERVAL=60", "FREQ=SECONDLY;INTERVAL=3600"]
)
async def test_start_arms_one_timer_per_active_task(rule: str) -> None:
    scheduler, _clock, seam, _fired = _make([_task("a", rule), _task("b", rule)])
    await scheduler.start()
    assert scheduler.job_count == 2
    assert len(seam.live()) == 2
    assert scheduler.next_run_at("a") is not None
    assert all(0 < timer.delay <= 3600 for timer in seam.live())


async def test_start_is_idempotent() -> None:
    # A second start() while already started must be a no-op: no duplicate
    # timers layered on top, and no re-load of the store.
    scheduler, _clock, seam, _fired = _make([_task("a"), _task("b")])
    await scheduler.start()
    jobs_after_first = scheduler.job_count
    timers_after_first = len(seam.timers)
    live_after_first = len(seam.live())

    await scheduler.start()  # second call — should do nothing

    assert scheduler.job_count == jobs_after_first == 2
    assert len(seam.timers) == timers_after_first  # no new timers armed
    assert len(seam.live()) == live_after_first == 2

    # stop() clears _started, so a subsequent start() re-arms cleanly.
    scheduler.stop()
    assert scheduler.job_count == 0
    await scheduler.start()
    assert scheduler.job_count == 2


async def test_start_skips_paused_tasks() -> None:
    scheduler, _clock, _seam, _fired = _make(
        [_task("a", state="active"), _task("b", state="paused")]
    )
    await scheduler.start()
    assert scheduler.job_count == 1


async def test_start_skips_bad_rrule_without_crashing() -> None:
    scheduler, _clock, _seam, _fired = _make(
        [_task("good", "FREQ=HOURLY"), _task("bad", "not an rrule")]
    )
    await scheduler.start()
    assert scheduler.job_count == 1
    assert scheduler.next_run_at("good") is not None
    assert scheduler.next_run_at("bad") is None


# ── firing ───────────────────────────────────────────────────────────────────


async def test_fire_invokes_on_fire_callback() -> None:
    scheduler, _clock, seam, fired = _make([_task("a", workspace_id=42)])
    await scheduler.start()
    await seam.fire_latest()
    assert fired.calls == [(42, "a")]


async def test_rearms_after_firing() -> None:
    scheduler, _clock, seam, _fired = _make([_task("a")])
    await scheduler.start()
    armed_before = len(seam.timers)
    await seam.fire_latest()
    # A fresh timer is armed for the next occurrence.
    assert len(seam.live()) == 1
    assert len(seam.timers) == armed_before + 1


# ── overlap SKIP ─────────────────────────────────────────────────────────────


async def test_overlapping_fire_is_skipped() -> None:
    # A slow on_fire that we can hold open while a second tick arrives.
    gate = asyncio.Event()
    calls: list[tuple[int, str]] = []

    async def slow_on_fire(workspace_id: int, task_id: str) -> None:
        calls.append((workspace_id, task_id))
        await gate.wait()

    scheduler, _clock, seam, _fired = _make([_task("a")], on_fire=slow_on_fire)
    await scheduler.start()

    first_timer = seam.live()[-1]
    first = asyncio.create_task(_fire_timer(first_timer))
    await asyncio.sleep(0)  # let slow_on_fire start and mark running

    # A second tick while the first is still running must be skipped.
    await scheduler.fire("a")
    assert calls == [(0, "a")]  # not fired twice

    gate.set()
    await first


# ── misfire grace ────────────────────────────────────────────────────────────


async def test_misfire_beyond_grace_is_skipped() -> None:
    scheduler, clock, seam, fired = _make([_task("a")])
    await scheduler.start()
    # The first fire is armed an hour out; jump the clock well past that
    # scheduled time + grace, then fire.
    clock.advance(3600 + MISFIRE_GRACE_TIME_S + 60)
    await seam.fire_latest()
    assert fired.calls == []  # dropped as a misfire
    # Still re-arms for the future.
    assert len(seam.live()) == 1


async def test_fire_within_grace_runs() -> None:
    scheduler, clock, seam, fired = _make([_task("a")])
    await scheduler.start()
    clock.advance(MISFIRE_GRACE_TIME_S - 1)
    await seam.fire_latest()
    assert fired.calls == [(0, "a")]


# ── CRUD sync ────────────────────────────────────────────────────────────────


async def test_add_registers_active_task() -> None:
    scheduler, _clock, _seam, _fired = _make([])
    await scheduler.start()
    assert scheduler.job_count == 0
    scheduler.add(_task("new"))
    assert scheduler.job_count == 1


async def test_add_ignores_paused_task() -> None:
    scheduler, _clock, _seam, _fired = _make([])
    await scheduler.start()
    scheduler.add(_task("p", state="paused"))
    assert scheduler.job_count == 0


async def test_remove_cancels_timer() -> None:
    scheduler, _clock, seam, _fired = _make([_task("a")])
    await scheduler.start()
    scheduler.remove("a")
    assert scheduler.job_count == 0
    assert len(seam.live()) == 0


async def test_update_reschedules() -> None:
    scheduler, _clock, _seam, _fired = _make([_task("a", "FREQ=HOURLY")])
    await scheduler.start()
    before = scheduler.next_run_at("a")
    scheduler.update(replace(scheduler_task_of(scheduler, "a"), rrule="FREQ=DAILY;BYHOUR=0"))
    after = scheduler.next_run_at("a")
    assert before != after
    assert scheduler.job_count == 1


async def test_update_removes_when_paused() -> None:
    scheduler, _clock, _seam, _fired = _make([_task("a")])
    await scheduler.start()
    scheduler.update(_task("a", state="paused"))
    assert scheduler.job_count == 0


# ── stop ─────────────────────────────────────────────────────────────────────


async def test_stop_cancels_all_timers() -> None:
    scheduler, _clock, seam, _fired = _make([_task("a"), _task("b")])
    await scheduler.start()
    scheduler.stop()
    assert scheduler.job_count == 0
    assert len(seam.live()) == 0


async def test_on_fire_exception_does_not_break_rearm() -> None:
    async def boom(workspace_id: int, task_id: str) -> None:
        raise RuntimeError("fire failed")

    scheduler, _clock, seam, _fired = _make([_task("a")], on_fire=boom)
    await scheduler.start()
    await seam.fire_latest()  # must not raise
    assert len(seam.live()) == 1  # re-armed despite the error


async def test_rearm_work_limit_clears_next_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    scheduler, clock, seam, fired = _make([_task("a")])
    await scheduler.start()
    clock.advance(seam.live()[0].delay)
    monkeypatch.setattr("omnigent.server.scheduled.rrule._MAX_SEARCH_STEPS", 0)
    await seam.fire_latest()
    assert fired.calls == [(0, "a")]
    assert scheduler.next_run_at("a") is None
    assert seam.live() == []
    assert "task a stopped" in caplog.text
    assert "work limit" in caplog.text


async def test_inactive_fire_does_not_remove_a_replacement_timer() -> None:
    reached, resume = asyncio.Event(), asyncio.Event()

    async def on_fire(workspace_id, task_id):
        reached.set()
        await resume.wait()
        return False

    scheduler, _clock, seam, _fired = _make([_task("a")], on_fire=on_fire)
    await scheduler.start()
    firing = asyncio.create_task(seam.fire_latest())
    try:
        await reached.wait()
        scheduler.update(_task("a", rrule="FREQ=DAILY"))
    finally:
        resume.set()
        await firing
    assert scheduler.job_count == 1
    assert len(seam.live()) == 1


# ── helpers ──────────────────────────────────────────────────────────────────


async def _fire_timer(timer) -> None:
    timer.cancelled = True
    result = timer.callback()
    if asyncio.iscoroutine(result):
        await result


def scheduler_task_of(scheduler: ScheduledTaskScheduler, task_id: str) -> _FakeTask:
    """Reach into the fake store to get the seed task for update tests."""
    return scheduler._store.get(task_id)  # type: ignore[attr-defined]


@pytest.mark.parametrize("mutation", ["create", "edit", "resume", "pause", "delete", "stop"])
async def test_startup_snapshot_preserves_changes_during_load(monkeypatch, mutation):
    from threading import Event

    original = _task("changed", state="paused" if mutation == "resume" else "active")
    scheduler, _clock, timers, _fired = _make([original, _task("untouched")])
    entered, release = asyncio.Event(), Event()
    loop = asyncio.get_running_loop()
    load = scheduler._store.list_active_all_workspaces

    def snapshot_then_wait():
        snapshot = load()
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(10), "Startup snapshot was never released"
        return snapshot

    monkeypatch.setattr(scheduler._store, "list_active_all_workspaces", snapshot_then_wait)
    startup = asyncio.create_task(scheduler.start())
    target = "new" if mutation == "create" else "changed"
    try:
        await asyncio.wait_for(entered.wait(), 10)
        if mutation == "create":
            scheduler.add(_task(target))
        elif mutation in ("edit", "resume"):
            scheduler.update(
                replace(
                    original, state="active", rrule="FREQ=DAILY;BYHOUR=9", timezone="Asia/Tokyo"
                )
            )
        elif mutation == "pause":
            scheduler.update(replace(original, state="paused"))
        elif mutation == "delete":
            scheduler.remove(target)
        else:
            scheduler.stop()
        expected = scheduler.next_run_at(target)
    finally:
        release.set()
        await startup
    try:
        assert scheduler.next_run_at(target) == expected
        assert (expected is not None) == (mutation in ("create", "edit", "resume"))
        assert scheduler.is_started == (mutation != "stop")
        assert (scheduler.next_run_at("untouched") is not None) == (mutation != "stop")
        assert len(timers.live()) == scheduler.job_count
    finally:
        scheduler.stop()
