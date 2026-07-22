"""In-process RRULE scheduler for recurring scheduled tasks.

:class:`ScheduledTaskScheduler` owns one self-rearming timer per active scheduled
task. It is the timing engine only: when a task is due it invokes an injected
``on_fire`` callback and immediately re-arms for the next occurrence. Creating
the agent session that actually runs the task is the callback's job — supplied
by the caller, never by this module.

Design notes:

* **Source of truth is the DB.** :meth:`ScheduledTaskScheduler.start` loads every
  active task via ``store.list_active_all_workspaces()`` and arms a timer for
  each. There is no in-memory schedule state beyond the live timers. Missed
  fires (server was down) are **not** replayed — only the next future occurrence
  is armed.
* **Timer overlap policy is SKIP.** If an ``on_fire`` callback for the same job
  is still running when the next tick arrives, the tick is dropped. The fire
  path also tracks its own longer-running session creation work.
* **Misfire grace.** A tick that arrives more than :data:`MISFIRE_GRACE_TIME_S`
  after its scheduled time (e.g. the event loop was blocked) is skipped.
* **Long-delay safety.** A single timer is capped at :data:`_MAX_TIMER_DELAY_S`
  and re-armed on wake, so annual schedules don't rely on one multi-month timer.

Timing seams (``now`` / ``schedule_call`` / ``cancel_call``) are injectable so
tests can drive the scheduler with a fake clock and manual timer firing.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from omnigent.entities import ScheduledTask
from omnigent.server.scheduled.rrule import (
    RRuleTrigger,
    RRuleValidationError,
    validate_rrule,
)

_logger = logging.getLogger(__name__)

_UTC = ZoneInfo("UTC")

# A tick that arrives more than this many seconds after its scheduled time is
# treated as a misfire and skipped (event loop was blocked, clock jumped, etc.).
MISFIRE_GRACE_TIME_S = 30

# Cap for a single armed timer. Longer waits (e.g. annual schedules) are armed
# in chunks: we wake at the cap, notice we're not due yet, and re-arm. 24 days.
_MAX_TIMER_DELAY_S = 24 * 24 * 60 * 60

# Slop allowed when deciding whether a capped-timer wake has actually reached
# the scheduled time.
_DUE_TOLERANCE_S = 1.0

# ``on_fire(workspace_id, scheduled_task_id)`` — invoked when a task is due. The
# caller creates the agent session under the provided workspace scope.
OnFire = Callable[[int, str], Awaitable[None]]
_JobKey = tuple[int, str]


class _ActiveTaskSource(Protocol):
    """The slice of ``ScheduledTaskStore`` the scheduler reads."""

    def list_active_all_workspaces(self) -> list[ScheduledTask]: ...


@dataclass
class _Job:
    """One registered task's live scheduling state."""

    task_id: str
    workspace_id: int
    trigger: RRuleTrigger
    tz: ZoneInfo
    next_run: datetime | None = None
    next_run_epoch: float | None = None
    timer: Any = None
    armed_capped: bool = False
    running: bool = False  # Drop overlapping on_fire callbacks.


def _resolve_tz(name: str | None) -> ZoneInfo:
    """Resolve an IANA timezone name, defaulting to UTC on missing/invalid."""
    if not name:
        return _UTC
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        _logger.warning("scheduler: unknown timezone %r, defaulting to UTC", name)
        return _UTC


class ScheduledTaskScheduler:
    """Arms one self-rearming timer per active scheduled task and fires the
    injected ``on_fire`` callback when each is due.

    :param store: Provides ``list_active_all_workspaces()`` for the boot-time
        schedule load.
    :param on_fire: Async callback invoked with ``(workspace_id,
        scheduled_task_id)`` when a task is due. Exceptions are caught and
        logged so a failing fire never stops the timer from re-arming.
    :param now: Returns the current epoch seconds. Injectable for tests;
        defaults to :func:`time.time`.
    :param schedule_call: Arms a timer: ``(delay_s, factory) -> handle`` where
        ``factory`` is a zero-arg callable returning the fire coroutine.
        Defaults to ``loop.call_later``.
    :param cancel_call: Cancels a handle returned by ``schedule_call``.
        Defaults to ``handle.cancel()``.
    """

    def __init__(
        self,
        store: _ActiveTaskSource,
        on_fire: OnFire,
        *,
        now: Callable[[], float] = time.time,
        schedule_call: Callable[[float, Callable[[], Any]], Any] | None = None,
        cancel_call: Callable[[Any], None] | None = None,
    ) -> None:
        self._store = store
        self._on_fire = on_fire
        self._now = now
        self._schedule_call = schedule_call or _default_schedule_call
        self._cancel_call = cancel_call or _default_cancel_call
        self._jobs: dict[_JobKey, _Job] = {}
        self._started = False

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """Load every active task and arm a timer for each.

        A persisted task with a bad RRULE is logged and skipped — it must never
        abort server startup. Idempotent: a second call while already started is
        a no-op (the store is not re-read and no timers are re-armed); call
        :meth:`stop` first if you need to reload.
        """
        if self._started:
            _logger.debug("scheduler: start() called but already started; ignoring")
            return
        for task in self._store.list_active_all_workspaces():
            try:
                self._register(task)
            except RRuleValidationError as exc:
                _logger.warning(
                    "scheduler: skipping task %s with invalid rrule %r: %s",
                    task.id,
                    task.rrule,
                    exc,
                )
        self._started = True
        _logger.info("ScheduledTaskScheduler started with %d job(s)", len(self._jobs))

    def stop(self) -> None:
        """Cancel every armed timer and drop all jobs."""
        for job in self._jobs.values():
            if job.timer is not None:
                self._cancel_call(job.timer)
        self._jobs.clear()
        self._started = False

    # ── CRUD sync (keeps timers in sync with row changes) ─────────────────────

    def add(self, task: ScheduledTask) -> None:
        """Register a task if it is active. No-op for paused/deleted tasks."""
        if task.state != "active":
            return
        try:
            self._register(task)
        except RRuleValidationError as exc:
            _logger.warning(
                "scheduler: cannot add task %s with invalid rrule %r: %s",
                task.id,
                task.rrule,
                exc,
            )

    def update(self, task: ScheduledTask) -> None:
        """Re-sync a task after a row change: drop then re-add (if active)."""
        self.remove(task.id)
        self.add(task)

    def remove(self, task_id: str) -> None:
        """Cancel and forget a task's timer. Idempotent."""
        for key, job in list(self._jobs.items()):
            if job.task_id == task_id:
                self._jobs.pop(key, None)
                if job.timer is not None:
                    self._cancel_call(job.timer)

    # ── introspection ─────────────────────────────────────────────────────────

    @property
    def job_count(self) -> int:
        """Number of currently registered jobs."""
        return len(self._jobs)

    @property
    def is_started(self) -> bool:
        """Whether :meth:`start` has run."""
        return self._started

    def next_run_at(self, task_id: str) -> str | None:
        """ISO-8601 timestamp of a task's next fire, or ``None`` if not armed."""
        job = next((j for j in self._jobs.values() if j.task_id == task_id), None)
        if job is None or job.next_run is None:
            return None
        return job.next_run.isoformat()

    # ── firing ─────────────────────────────────────────────────────────────────

    async def fire(self, task_id: str) -> bool:
        """Fire a task immediately, respecting the overlap (SKIP) policy.

        Used by tests and by an out-of-band trigger; the timer path uses the
        internal fire-and-rearm. Returns ``True`` if ``on_fire`` was invoked.

        :param task_id: The task to fire.
        :returns: ``True`` if fired, ``False`` if skipped or unknown.
        """
        job = next((j for j in self._jobs.values() if j.task_id == task_id), None)
        if job is None:
            return False
        return await self._fire_job(job, scheduled_epoch=self._now())

    # ── internals ────────────────────────────────────────────────────────────

    def _register(self, task: ScheduledTask) -> None:
        """Validate the task's rrule and arm its timer, replacing any existing."""
        trigger = validate_rrule(task.rrule)
        self.remove(task.id)  # replace_existing semantics
        job = _Job(
            task_id=task.id,
            workspace_id=task.workspace_id,
            trigger=trigger,
            tz=_resolve_tz(task.timezone),
        )
        self._jobs[(task.workspace_id, task.id)] = job
        self._arm(job)

    def _arm(self, job: _Job) -> None:
        """Compute the next fire and arm a (possibly capped) timer for it."""
        now_epoch = self._now()
        after = datetime.fromtimestamp(now_epoch, tz=_UTC)
        next_run = job.trigger.next_fire_after(after, job.tz)
        if next_run is None:
            job.next_run = None
            job.next_run_epoch = None
            job.timer = None
            return
        job.next_run = next_run
        job.next_run_epoch = next_run.timestamp()

        delay = job.next_run_epoch - now_epoch
        if delay < 0:
            delay = 0.0
        job.armed_capped = delay > _MAX_TIMER_DELAY_S
        if job.armed_capped:
            delay = _MAX_TIMER_DELAY_S
        job.timer = self._schedule_call(delay, lambda: self._fire_and_rearm(job))

    async def _fire_and_rearm(self, job: _Job) -> None:
        """Timer callback: fire if due, then always re-arm for the next slot."""
        try:
            now_epoch = self._now()
            scheduled = job.next_run_epoch
            # A capped timer wakes before the real fire time — re-arm, don't fire.
            early_wake = (
                job.armed_capped
                and scheduled is not None
                and scheduled - now_epoch > _DUE_TOLERANCE_S
            )
            if not early_wake and scheduled is not None:
                await self._fire_job(job, scheduled_epoch=scheduled)
        finally:
            # Only re-arm if the job is still registered (it may have been
            # removed mid-fire).
            if self._jobs.get((job.workspace_id, job.task_id)) is job:
                self._arm(job)

    async def _fire_job(self, job: _Job, *, scheduled_epoch: float) -> bool:
        """Invoke ``on_fire`` for a job, applying overlap + misfire policy.

        :returns: ``True`` if ``on_fire`` was invoked, ``False`` if skipped.
        """
        if job.running:
            _logger.debug("scheduler: task %s still running, skipping tick", job.task_id)
            return False
        now_epoch = self._now()
        if now_epoch - scheduled_epoch > MISFIRE_GRACE_TIME_S:
            _logger.info(
                "scheduler: task %s misfire (%.0fs late), skipping",
                job.task_id,
                now_epoch - scheduled_epoch,
            )
            return False
        job.running = True
        try:
            await self._on_fire(job.workspace_id, job.task_id)
            return True
        except Exception:
            _logger.exception("scheduler: on_fire for task %s failed", job.task_id)
            return False
        finally:
            job.running = False


# Strong references to in-flight fire coroutines. ``loop.create_task`` only
# holds a weak reference, so without this a fire could be garbage-collected
# mid-flight; we discard each task from the set when it completes.
_PENDING_FIRES: set[Any] = set()


def _default_schedule_call(delay: float, factory: Callable[[], Any]) -> Any:
    """Arm a real ``loop.call_later`` timer that spawns the fire coroutine."""
    import asyncio

    # All arm sites run inside the event loop (start() and the timer callback),
    # so the running loop is always available.
    loop = asyncio.get_running_loop()

    def _tick() -> None:
        result = factory()
        if result is not None:
            task = asyncio.ensure_future(result)
            _PENDING_FIRES.add(task)
            task.add_done_callback(_PENDING_FIRES.discard)

    return loop.call_later(delay, _tick)


def _default_cancel_call(handle: Any) -> None:
    """Cancel a ``loop.call_later`` handle."""
    handle.cancel()
