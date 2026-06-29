"""Tests for the tokenmaxx off-hours orchestrator engine (#11).

Covers the pure gating/selection helpers (off-hours window incl. midnight wrap,
planned-before-new ordering + cap) and the ``tick`` loop (disabled/daytime
no-ops, dispatch-up-to-cap + mark-in_progress, quota-headroom cap, and that a
failed dispatch is left un-marked for the next tick). Clock, store, dispatch,
and headroom are all injected — no live server needed.
"""

from __future__ import annotations

from datetime import datetime

from omnigent.entities.work_item import WorkItem
from omnigent.runtime.tokenmaxx import (
    TokenmaxxConfig,
    TokenmaxxService,
    is_off_hours,
    select_for_dispatch,
)


def _wi(wid: str, status: str) -> WorkItem:
    return WorkItem(
        id=wid, source="manual", title=f"t-{wid}", dedup_key=wid, status=status, created_at=0
    )


class _FakeStore:
    def __init__(self, items: list[WorkItem]) -> None:
        self._items = items
        self.updated: list[tuple[str, str | None]] = []

    def list(self, *, status=None, conversation_id=None, limit=200):
        rows = [i for i in self._items if status is None or i.status == status]
        return rows[:limit]

    def update(self, work_item_id, *, status=None, **_):
        self.updated.append((work_item_id, status))
        for i in self._items:
            if i.id == work_item_id and status:
                i.status = status
        return


async def _dispatch_ok(_item: WorkItem) -> bool:
    return True


def _night() -> datetime:
    return datetime(2026, 6, 29, 2, 0)  # 02:00 — inside a 22→6 window


# ── is_off_hours ──────────────────────────────────────────────────────────
def test_is_off_hours_overnight_wrap() -> None:
    expectations = {23: True, 2: True, 5: True, 6: False, 21: False, 22: True, 12: False}
    for hour, expected in expectations.items():
        assert is_off_hours(datetime(2026, 6, 29, hour, 0), 22, 6) is expected


def test_is_off_hours_same_day_window() -> None:
    assert is_off_hours(datetime(2026, 6, 29, 10, 0), 9, 17) is True
    assert is_off_hours(datetime(2026, 6, 29, 8, 0), 9, 17) is False
    assert is_off_hours(datetime(2026, 6, 29, 17, 0), 9, 17) is False  # end is exclusive


def test_is_off_hours_empty_window() -> None:
    assert is_off_hours(datetime(2026, 6, 29, 5, 0), 5, 5) is False


# ── select_for_dispatch ───────────────────────────────────────────────────
def test_select_orders_planned_before_new_and_caps() -> None:
    items = [
        _wi("n1", "new"),
        _wi("p1", "planned"),
        _wi("n2", "new"),
        _wi("p2", "planned"),
        _wi("d", "done"),
    ]
    assert [i.id for i in select_for_dispatch(items, 3)] == ["p1", "p2", "n1"]


def test_select_cap_zero_is_empty() -> None:
    assert select_for_dispatch([_wi("p", "planned")], 0) == []


# ── TokenmaxxService.tick ─────────────────────────────────────────────────
async def test_tick_noop_when_disabled() -> None:
    store = _FakeStore([_wi("p", "planned")])
    svc = TokenmaxxService(store, _dispatch_ok, TokenmaxxConfig(enabled=False), now=_night)
    assert await svc.tick() == 0
    assert store.updated == []


async def test_tick_noop_during_daytime() -> None:
    store = _FakeStore([_wi("p", "planned")])
    svc = TokenmaxxService(
        store,
        _dispatch_ok,
        TokenmaxxConfig(enabled=True, off_hours_start=22, off_hours_end=6),
        now=lambda: datetime(2026, 6, 29, 12, 0),
    )
    assert await svc.tick() == 0
    assert store.updated == []


async def test_tick_dispatches_up_to_cap_planned_first_and_marks() -> None:
    store = _FakeStore(
        [_wi("p1", "planned"), _wi("p2", "planned"), _wi("n1", "new"), _wi("n2", "new")]
    )
    svc = TokenmaxxService(
        store, _dispatch_ok, TokenmaxxConfig(enabled=True, max_items_per_tick=2), now=_night
    )
    assert await svc.tick() == 2
    assert {wid for wid, _ in store.updated} == {"p1", "p2"}
    assert [s for _, s in store.updated] == ["in_progress", "in_progress"]


async def test_tick_headroom_caps_below_max() -> None:
    store = _FakeStore([_wi("p1", "planned"), _wi("p2", "planned")])

    async def headroom() -> int:
        return 1

    svc = TokenmaxxService(
        store,
        _dispatch_ok,
        TokenmaxxConfig(enabled=True, max_items_per_tick=5),
        headroom=headroom,
        now=_night,
    )
    assert await svc.tick() == 1


async def test_tick_zero_headroom_dispatches_nothing() -> None:
    store = _FakeStore([_wi("p1", "planned")])

    async def headroom() -> int:
        return 0

    svc = TokenmaxxService(
        store, _dispatch_ok, TokenmaxxConfig(enabled=True), headroom=headroom, now=_night
    )
    assert await svc.tick() == 0
    assert store.updated == []


async def test_tick_failed_dispatch_left_unmarked() -> None:
    store = _FakeStore([_wi("p1", "planned")])

    async def dispatch_fail(_item: WorkItem) -> bool:
        return False

    svc = TokenmaxxService(store, dispatch_fail, TokenmaxxConfig(enabled=True), now=_night)
    assert await svc.tick() == 0
    assert store.updated == []
