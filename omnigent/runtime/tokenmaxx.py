"""Tokenmaxx (#11) — off-hours work-item orchestrator.

During a configured off-hours window, and while there's quota headroom, this
service pulls ``new`` / ``planned`` work items and dispatches them to agents,
marking each ``in_progress``. It turns the work-items backlog (#3) into useful
overnight throughput against otherwise-idle subscription quota.

Design mirrors :class:`~omnigent.runtime.scheduler.SchedulerService`: the
*dispatch* and *quota headroom* are injected callbacks and the clock/sleep are
injectable, so the engine — off-hours gating, quota cap, selection, and the
dispatch-and-mark loop — is host-agnostic and fully unit-testable. The
production dispatch runs a turn in the item's conversation; the production
headroom derives from usage (#10). Per-turn token ceilings stay the job of the
existing ``cost_budget`` policy — tokenmaxx only decides *what* and *when*.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime

from omnigent.entities.work_item import WorkItem
from omnigent.stores.work_item_store import WorkItemStore

_logger = logging.getLogger(__name__)

# Returns True if the item's turn was accepted for execution (so it should be
# marked in_progress); False means "couldn't dispatch" (leave it for next tick).
DispatchCallback = Callable[[WorkItem], Awaitable[bool]]
# Returns the maximum number of dispatches quota allows *this tick* (0 = none).
HeadroomCallback = Callable[[], Awaitable[int]]

# Statuses tokenmaxx will pick up, in dispatch priority order: a ``planned``
# item already has a plan to execute, so it goes before a raw ``new`` one.
_DISPATCHABLE_ORDER = ("planned", "new")


def _local_now() -> datetime:
    """:returns: The current local wall-clock time (off-hours is local)."""
    return datetime.now()


@dataclass(frozen=True)
class TokenmaxxConfig:
    """Operator settings for the off-hours orchestrator (the ``tokenmaxx:`` key).

    :param enabled: Master switch. ``False`` (default) → the service never
        starts; zero behaviour change.
    :param off_hours_start: Inclusive local start hour [0–23] of the window.
    :param off_hours_end: Exclusive local end hour [0–23]. A window that wraps
        midnight (start > end, e.g. 22→6) is supported.
    :param max_items_per_tick: Upper bound on dispatches per tick (further
        capped by quota headroom).
    :param tick_seconds: Seconds between ticks.
    """

    enabled: bool = False
    off_hours_start: int = 22
    off_hours_end: int = 6
    max_items_per_tick: int = 3
    tick_seconds: int = 900


def is_off_hours(now: datetime, start_hour: int, end_hour: int) -> bool:
    """Whether ``now`` falls in the [start, end) local-hour window.

    Handles a window that wraps midnight (``start_hour > end_hour``). An empty
    window (``start_hour == end_hour``) is never off-hours.

    :param now: The current local time.
    :param start_hour: Inclusive start hour.
    :param end_hour: Exclusive end hour.
    :returns: ``True`` when ``now``'s hour is inside the window.
    """
    hour = now.hour
    if start_hour == end_hour:
        return False
    if start_hour < end_hour:
        return start_hour <= hour < end_hour
    # Wraps midnight: in-window if at/after start OR before end.
    return hour >= start_hour or hour < end_hour


def select_for_dispatch(items: list[WorkItem], cap: int) -> list[WorkItem]:
    """Order dispatchable items (``planned`` before ``new``) and cap the count.

    :param items: Candidate work items (any statuses; non-dispatchable dropped).
    :param cap: Maximum number to return.
    :returns: Up to ``cap`` items, ``planned`` first then ``new``.
    """
    if cap <= 0:
        return []
    ordered: list[WorkItem] = []
    for status in _DISPATCHABLE_ORDER:
        ordered.extend(i for i in items if i.status == status)
    return ordered[:cap]


class TokenmaxxService:
    """Ticks during off-hours and dispatches the work-items backlog.

    :param store: Work-item persistence (read dispatchable, mark in_progress).
    :param dispatch: Awaitable run per item; returns ``True`` if accepted.
    :param config: Operator settings (window, caps, cadence, enabled).
    :param headroom: Optional quota gate returning the max dispatches allowed
        this tick; ``None`` means "quota-unbounded" (only ``max_items_per_tick``
        applies).
    :param now: Injectable local clock (tests).
    :param sleep: Injectable async sleep (tests).
    """

    def __init__(
        self,
        store: WorkItemStore,
        dispatch: DispatchCallback,
        config: TokenmaxxConfig,
        *,
        headroom: HeadroomCallback | None = None,
        now: Callable[[], datetime] = _local_now,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store = store
        self._dispatch = dispatch
        self._config = config
        self._headroom = headroom
        self._now = now
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        """Start the tick loop. No-op when disabled or already running."""
        if not self._config.enabled or self._task is not None:
            return
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        """Cancel the tick loop and await its teardown."""
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task

    async def _run_loop(self) -> None:
        """Sleep one cadence → tick → repeat, until cancelled."""
        while True:
            await self._sleep(self._config.tick_seconds)
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed tick must not kill the loop — log and keep cadence.
                _logger.exception("tokenmaxx: tick failed")

    async def tick(self) -> int:
        """Run one off-hours pass: select within quota, dispatch, mark.

        Public (not just loop-internal) so tests drive it directly with an
        injected clock, store, dispatch, and headroom.

        :returns: The number of items dispatched this tick.
        """
        if not self._config.enabled:
            return 0
        if not is_off_hours(
            self._now(), self._config.off_hours_start, self._config.off_hours_end
        ):
            return 0

        cap = self._config.max_items_per_tick
        if self._headroom is not None:
            cap = min(cap, await self._headroom())
        if cap <= 0:
            _logger.debug("tokenmaxx: no quota headroom this tick")
            return 0

        # Fetch enough of each dispatchable status to fill the cap.
        candidates: list[WorkItem] = []
        for status in _DISPATCHABLE_ORDER:
            candidates.extend(
                await asyncio.to_thread(self._store.list, status=status, limit=cap)
            )
        selected = select_for_dispatch(candidates, cap)
        if not selected:
            return 0

        dispatched = 0
        for item in selected:
            try:
                accepted = await self._dispatch(item)
            except Exception:
                _logger.exception("tokenmaxx: dispatch failed for work item %s", item.id)
                continue
            if accepted:
                await asyncio.to_thread(self._store.update, item.id, status="in_progress")
                dispatched += 1
        if dispatched:
            _logger.info("tokenmaxx: dispatched %d work item(s) this tick", dispatched)
        return dispatched
