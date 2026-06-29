"""Server-side scheduler engine for cron ``loop`` schedules.

This is the engine half of the scheduler (B1): it arms enabled ``loop``
schedules and, on each cron tick, invokes an injected ``fire`` callback and
records ``last_fired_at``. The callback that actually runs a turn in the
conversation — driving omnigent's host-launch / turn-dispatch path — is wired
separately and started from the server lifespan (B2). Keeping the trigger
injected makes this module **host-agnostic and fully unit-testable** (the
clock, the sleep, and the fire callback are all injectable).

``monitor`` schedules are intentionally NOT handled here: a monitor streams a
shell command in the *host's* workspace, so it belongs to the host/runner
side, not this server-resident cron engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone

from croniter import croniter

from omnigent.entities.schedule import Schedule
from omnigent.stores.schedule_store import ScheduleStore

_logger = logging.getLogger(__name__)

# Called once per cron tick with the schedule that fired. The real
# implementation (B2) starts a turn in ``schedule.conversation_id``.
FireCallback = Callable[[Schedule], Awaitable[None]]


def _utcnow() -> datetime:
    """:returns: The current time as a timezone-aware UTC ``datetime``."""
    return datetime.now(timezone.utc)


class SchedulerService:
    """Arms enabled ``loop`` schedules and fires them on their cron cadence.

    :param store: Schedule persistence (read enabled rows, stamp last-fired).
    :param fire: Awaitable invoked on each tick with the firing schedule.
    :param now: Injectable clock returning aware-UTC ``datetime`` (tests).
    :param sleep: Injectable async sleep (tests); defaults to ``asyncio.sleep``.
    """

    def __init__(
        self,
        store: ScheduleStore,
        fire: FireCallback,
        *,
        now: Callable[[], datetime] = _utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._fire = fire
        self._now = now
        self._sleep = sleep
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._started = False

    async def start(self) -> None:
        """Arm all currently-enabled loop schedules. Idempotent."""
        if self._started:
            return
        self._started = True
        await self.refresh()

    async def refresh(self) -> None:
        """Reconcile armed tasks with the store's enabled loop schedules.

        Call after a schedule is created, deleted, enabled, or disabled so the
        live set of cron tasks matches persistence. Arms newly-enabled loops
        and cancels tasks whose schedule is gone or disabled.
        """
        enabled = await asyncio.to_thread(self._store.list_enabled)
        loops = {s.id: s for s in enabled if s.kind == "loop" and s.cron}
        for schedule_id in list(self._tasks):
            if schedule_id not in loops:
                await self._cancel(schedule_id)
        for schedule_id, schedule in loops.items():
            if schedule_id not in self._tasks:
                self._tasks[schedule_id] = asyncio.create_task(self._run_loop(schedule))

    async def stop(self) -> None:
        """Cancel every armed task and await its teardown."""
        for schedule_id in list(self._tasks):
            await self._cancel(schedule_id)
        self._started = False

    async def _cancel(self, schedule_id: str) -> None:
        """Cancel and await one armed task, if present.

        :param schedule_id: The schedule whose task to cancel.
        """
        task = self._tasks.pop(schedule_id, None)
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    def _seconds_until_next(self, cron: str) -> float:
        """Seconds from now until the next time ``cron`` is due.

        :param cron: A 5-field cron expression, e.g. ``"0 22 * * FRI"``.
        :returns: Non-negative seconds until the next occurrence.
        :raises ValueError: If ``cron`` is not a valid expression.
        """
        base = self._now()
        try:
            nxt: datetime = croniter(cron, base).get_next(datetime)
        except (ValueError, KeyError) as exc:
            raise ValueError(f"invalid cron {cron!r}: {exc}") from exc
        return max(0.0, (nxt - base).total_seconds())

    async def _run_loop(self, schedule: Schedule) -> None:
        """Sleep-until-due → fire → stamp → repeat, until cancelled.

        :param schedule: The loop schedule to run (must have a cron).
        """
        cron = schedule.cron
        if not cron:
            return
        try:
            delay = self._seconds_until_next(cron)
        except ValueError:
            _logger.exception("scheduler: skipping schedule %s with invalid cron", schedule.id)
            return
        while True:
            await self._sleep(delay)
            try:
                await self._fire(schedule)
                await asyncio.to_thread(
                    self._store.update,
                    schedule.id,
                    last_fired_at=int(self._now().timestamp()),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed fire must not kill the loop — log and keep the
                # cadence so the next tick still runs.
                _logger.exception("scheduler: fire failed for schedule %s", schedule.id)
            delay = self._seconds_until_next(cron)
