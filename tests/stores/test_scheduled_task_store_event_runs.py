"""Event-triggered tasks in :class:`SqlAlchemyScheduledTaskStore`.

Covers the shared trigger schema (``trigger_config``, a nullable ``rrule``, the
run ``event``) and recording runs for provider events: occurrence-key dedupe,
coalescing into a continued task's pending run, the per-minute cap, and claims.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from omnigent.entities import ScheduledTask
from omnigent.server.scheduled.schedule import event_trigger_key, occurrence_host_id
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore

_TRIGGER = {"provider": "omnigent", "events": ["turn.completed"]}


def _uid(seed: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyScheduledTaskStore:
    store = SqlAlchemyScheduledTaskStore(db_uri)
    store.configure_dispatch(True)
    return store


def _event_task(
    store: SqlAlchemyScheduledTaskStore, seed: str = "event", **kwargs: Any
) -> ScheduledTask:
    task = store.create(
        _uid(seed),
        seed,
        "Review the session",
        None,
        "alice@example.com",
        _uid("agent"),
        "UTC",
        host_id=_uid("host"),
        trigger_config=_TRIGGER,
        **kwargs,
    )
    stored = store.get(task.id)
    assert stored is not None
    return stored


def _record(
    store: SqlAlchemyScheduledTaskStore,
    task: ScheduledTask,
    occurrence: str,
    *,
    now: int = 1_000,
    coalesce: bool = False,
    cap: int = 10,
) -> str:
    key = event_trigger_key("omnigent", occurrence)
    return store.record_event_run(
        task,
        now=now,
        trigger_key=key,
        host_id=occurrence_host_id(task, key),
        event={"type": "turn.completed", "session_id": occurrence},
        coalesce=coalesce,
        max_runs_per_minute=cap,
    )


def test_an_event_task_has_no_rule_and_no_next_run(store: SqlAlchemyScheduledTaskStore) -> None:
    task = _event_task(store)
    assert (task.rrule, task.trigger_config) == (None, _TRIGGER)
    # Persistent dispatch neither schedules nor completes it.
    assert (task.state, task.next_run_at) == ("active", None)
    assert store.list_uninitialized_tasks() == []
    assert store.list_event_triggered_tasks() == [task]
    assert store.update(task.id, state="paused").state == "paused"
    assert store.list_event_triggered_tasks() == []


def test_a_task_needs_exactly_one_of_rule_and_trigger(store: SqlAlchemyScheduledTaskStore) -> None:
    with pytest.raises(ValueError, match="exactly one"):
        store.create(_uid("x"), "x", "p", None, None, _uid("agent"), "UTC")
    with pytest.raises(ValueError, match="exactly one"):
        store.create(
            _uid("y"), "y", "p", "FREQ=DAILY", None, _uid("agent"), "UTC", trigger_config=_TRIGGER
        )


def test_switching_between_schedule_and_events(store: SqlAlchemyScheduledTaskStore) -> None:
    scheduled = store.create(_uid("s"), "s", "p", "FREQ=HOURLY", None, _uid("agent"), "UTC")
    assert scheduled.next_run_at is not None
    evented = store.update(scheduled.id, trigger_config=_TRIGGER)
    assert (evented.rrule, evented.trigger_config, evented.next_run_at) == (None, _TRIGGER, None)
    # Removing the trigger needs a rule to fall back on.
    with pytest.raises(ValueError, match="requires an rrule"):
        store.update(scheduled.id, trigger_config=None)
    back = store.update(scheduled.id, rrule="FREQ=DAILY", trigger_config=None)
    assert (back.rrule, back.trigger_config) == ("FREQ=DAILY", None)
    assert back.next_run_at is not None


def test_event_runs_wait_as_pending_runs_and_drop_repeats(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    task = _event_task(store)
    assert _record(store, task, "s1") == "recorded"
    assert _record(store, task, "s1") == "duplicate"
    [pending] = store.list_pending_runs()
    assert (pending.status, pending.lease_expires_at) == ("scheduled", None)
    assert pending.trigger_key == event_trigger_key("omnigent", "s1")
    assert pending.event == {
        "events": [{"type": "turn.completed", "session_id": "s1", "key": pending.trigger_key}]
    }
    claimed = store.claim_pending_run(pending, now=1_001, lease_seconds=120)
    assert claimed is not None
    assert claimed[1].event == pending.event
    # A claimed occurrence still counts as recorded.
    assert _record(store, task, "s1") == "duplicate"


def test_a_continued_task_coalesces_events_into_its_pending_run(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    task = _event_task(store, session_mode="continue")
    assert _record(store, task, "s1", coalesce=True) == "recorded"
    assert _record(store, task, "s2", coalesce=True) == "coalesced"
    assert _record(store, task, "s2", coalesce=True) == "duplicate"
    [pending] = store.list_pending_runs()
    assert [e["session_id"] for e in pending.event["events"]] == ["s1", "s2"]
    # Past the listed maximum, events are only counted.
    for index in range(30):
        assert _record(store, task, f"more{index}", coalesce=True) == "coalesced"
    [pending] = store.list_pending_runs()
    assert len(pending.event["events"]) == 20
    assert pending.event["omitted"] == 12
    # Once claimed, the next event starts a new pending run.
    assert store.claim_pending_run(pending, now=1_001, lease_seconds=120) is not None
    assert _record(store, task, "s3", coalesce=True) == "recorded"


def test_the_per_minute_cap_drops_events_and_records_one_skip(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    task = _event_task(store)
    assert [_record(store, task, f"s{i}", cap=2) for i in range(4)] == [
        "recorded",
        "recorded",
        "rate_limited",
        "rate_limited",
    ]
    runs, _ = store.list_runs(task.id)
    skipped = [run for run in runs if run.status == "skipped"]
    assert [(run.error_code, run.trigger_key) for run in skipped] == [
        ("rate_limited", f"rate_limited:{1_000 // 60}")
    ]
    # The window moves on, and the skip itself does not count.
    assert _record(store, task, "later", now=1_061, cap=2) == "recorded"


def test_an_inactive_task_records_nothing_and_its_pending_events_skip(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    task = _event_task(store)
    assert _record(store, task, "s1") == "recorded"
    [pending] = store.list_pending_runs()
    paused = store.update(task.id, state="paused")
    assert _record(store, paused, "s2") == "inactive"
    # Unlike Run now, an event recorded before the pause no longer starts.
    assert store.claim_pending_run(pending, now=1_001, lease_seconds=120) is None
    [run] = store.list_runs(task.id)[0]
    assert (run.status, run.error_code) == ("skipped", "trigger_inactive")
    # A Run now request still starts the paused task.
    manual = store.request_manual_run(
        paused, now=1_002, trigger_key="manual:x", host_id=paused.host_id
    )
    assert store.claim_pending_run(manual, now=1_003, lease_seconds=120) is not None


def test_a_scheduled_task_records_no_event_runs(store: SqlAlchemyScheduledTaskStore) -> None:
    task = store.create(_uid("s"), "s", "p", "FREQ=HOURLY", None, _uid("agent"), "UTC")
    assert _record(store, task, "s1") == "inactive"
    with pytest.raises(ValueError, match="must start with"):
        store.record_event_run(
            task,
            now=1,
            trigger_key="manual:x",
            host_id=None,
            event={},
            coalesce=False,
            max_runs_per_minute=1,
        )
