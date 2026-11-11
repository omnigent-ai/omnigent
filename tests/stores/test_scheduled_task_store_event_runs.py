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
    kwargs.setdefault("trigger_config", _TRIGGER)
    task = store.create(
        _uid(seed),
        seed,
        "Review the session",
        None,
        "alice@example.com",
        _uid("agent"),
        "UTC",
        host_id=_uid("host"),
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
    assert store.list_event_triggered_tasks(provider="omnigent") == [task]
    # The provider is stored as the scalar that discovery seeks on; next_run_at stays NULL.
    from sqlalchemy import select

    from omnigent.db.db_models import SqlScheduledTask

    with store._session("read_provider") as session:
        stored = session.execute(
            select(SqlScheduledTask.trigger_provider, SqlScheduledTask.next_run_at)
        ).one()
    assert tuple(stored) == ("omnigent", None)
    assert store.list_event_triggered_tasks(provider="jira") == []
    assert store.update(task.id, state="paused").state == "paused"
    assert store.list_event_triggered_tasks(provider="omnigent") == []


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
    # Once claimed, the next event starts a new pending run (after the minute's
    # 32 accepted events no longer count toward the cap).
    assert store.claim_pending_run(pending, now=1_001, lease_seconds=120) is not None
    assert _record(store, task, "s3", now=1_061, coalesce=True) == "recorded"
    # Occurrences that joined the claimed run, listed or only counted, stay received.
    assert _record(store, task, "s2", now=1_062, coalesce=True) == "duplicate"
    assert _record(store, task, "more29", now=1_062, coalesce=True) == "duplicate"


def test_a_coalesced_run_stays_within_its_column(store: SqlAlchemyScheduledTaskStore) -> None:
    task = _event_task(store, session_mode="continue")
    wide = "x" * 4_000
    key = event_trigger_key("omnigent", "first")
    for index in range(5):
        key = event_trigger_key("omnigent", f"wide{index}")
        assert store.record_event_run(
            task,
            now=1_000,
            trigger_key=key,
            host_id=task.host_id,
            event={"type": "turn.completed", "session_id": wide},
            coalesce=True,
            max_runs_per_minute=10,
        ) in {"recorded", "coalesced"}
    [pending] = store.list_pending_runs()
    # Entries that would push the document past its bound are counted instead.
    assert len(pending.event["events"]) == 2
    assert pending.event["omitted"] == 3


def test_a_document_at_its_bound_still_takes_the_omitted_count(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    from omnigent.stores.scheduled_task_store import sqlalchemy_store

    task = _event_task(store, session_mode="continue")
    limit = sqlalchemy_store._MAX_JSON_DOCUMENT_CHARS - sqlalchemy_store._OMITTED_FIELD_RESERVE

    def record(key: str, filler: int) -> str:
        return store.record_event_run(
            task,
            now=1_000,
            trigger_key=event_trigger_key("omnigent", key),
            host_id=task.host_id,
            event={"type": "turn.completed", "session_id": "x" * filler},
            coalesce=True,
            max_runs_per_minute=10,
        )

    assert record("first", 10) == "recorded"
    [pending] = store.list_pending_runs()
    used = len(sqlalchemy_store._dump_json(pending.event) or "")
    # An entry that fills the listed document exactly to its limit is listed...
    entry_overhead = len(
        sqlalchemy_store._dump_json(
            {
                "events": [
                    *pending.event["events"],
                    {
                        "type": "turn.completed",
                        "session_id": "",
                        "key": event_trigger_key("omnigent", "second"),
                    },
                ]
            }
        )
        or ""
    )
    assert record("second", limit - entry_overhead) == "coalesced"
    [pending] = store.list_pending_runs()
    assert len(sqlalchemy_store._dump_json(pending.event) or "") == limit > used
    # ...and the next one is counted, which still fits within the column.
    for index in range(50):
        assert record(f"more{index}", 10) == "coalesced"
    [pending] = store.list_pending_runs()
    assert pending.event["omitted"] == 50


def test_an_event_too_large_to_list_is_counted(store: SqlAlchemyScheduledTaskStore) -> None:
    task = _event_task(store)
    assert (
        store.record_event_run(
            task,
            now=1_000,
            trigger_key=event_trigger_key("omnigent", "huge"),
            host_id=task.host_id,
            event={"type": "turn.completed", "session_id": "x" * 20_000},
            coalesce=False,
            max_runs_per_minute=10,
        )
        == "recorded"
    )
    [pending] = store.list_pending_runs()
    assert pending.event == {"events": [], "omitted": 1}


def test_an_oversized_trigger_is_refused(store: SqlAlchemyScheduledTaskStore) -> None:
    with pytest.raises(ValueError, match="exceeds"):
        _event_task(store, "big", trigger_config={"provider": "omnigent", "x": "y" * 13_000})


def test_event_tasks_are_listed_in_id_pages(store: SqlAlchemyScheduledTaskStore) -> None:
    tasks = sorted((_event_task(store, f"page{i}") for i in range(5)), key=lambda t: t.id)
    store.create(_uid("scheduled"), "s", "p", "FREQ=HOURLY", None, _uid("agent"), "UTC")
    first = store.list_event_triggered_tasks(provider="omnigent", limit=2)
    second = store.list_event_triggered_tasks(provider="omnigent", after=first[-1].id, limit=2)
    third = store.list_event_triggered_tasks(provider="omnigent", after=second[-1].id, limit=2)
    assert [t.id for t in [*first, *second, *third]] == [t.id for t in tasks]


def test_deleting_a_task_removes_its_receipts(store: SqlAlchemyScheduledTaskStore) -> None:
    from sqlalchemy import func, select

    from omnigent.db.db_models import SqlScheduledTaskEventReceipt

    store._cleanup_batch_size = 2
    task = _event_task(store, session_mode="continue")
    for index in range(5):
        _record(store, task, f"s{index}", coalesce=True)
    assert store.delete(task.id)
    with store._session("count_receipts") as session:
        remaining = session.scalar(select(func.count()).select_from(SqlScheduledTaskEventReceipt))
    assert remaining == 0
    assert store.get(task.id) is None


def test_old_receipts_expire_as_events_arrive(store: SqlAlchemyScheduledTaskStore) -> None:
    from sqlalchemy import select

    from omnigent.db.db_models import SqlScheduledTaskEventReceipt

    task = _event_task(store)
    assert _record(store, task, "old", now=1_000) == "recorded"
    later = 1_000 + 8 * 24 * 3600
    assert _record(store, task, "new", now=later) == "recorded"
    with store._session("list_receipts") as session:
        keys = session.scalars(select(SqlScheduledTaskEventReceipt.trigger_key)).all()
    assert keys == [event_trigger_key("omnigent", "new")]
    # The run keyed by the expired occurrence still drops a repeat.
    assert _record(store, task, "old", now=later + 120) == "duplicate"


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


def test_the_cap_counts_accepted_events_not_other_runs(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    task = _event_task(store)
    # Skipped runs and Run now requests in the window don't use the event cap.
    for index in range(30):
        store.create_run(uuid.uuid4().hex, task.id, "skipped", 1_000, finished_at=1_000)
        store.request_manual_run(task, now=1_000, trigger_key=f"manual:{index}", host_id=None)
    assert _record(store, task, "s1", cap=1) == "recorded"
    assert _record(store, task, "s2", cap=1) == "rate_limited"


def test_a_trigger_changed_since_it_was_listed_records_nothing(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    listed = _event_task(store)
    store.update(listed.id, trigger_config={"provider": "omnigent", "events": ["comment.created"]})
    # The worker matched the event against its cached copy of the old trigger.
    assert _record(store, listed, "s1") == "inactive"
    assert _record(store, store.get(listed.id), "s1") == "recorded"


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


def test_events_in_one_second_join_the_waiting_run(store: SqlAlchemyScheduledTaskStore) -> None:
    task = _event_task(store, session_mode="continue")
    # A's run is claimed; B starts the next run; C, in the same second, joins B's run
    # whichever receipt sorts last.
    assert _record(store, task, "a", coalesce=True) == "recorded"
    [first] = store.list_pending_runs()
    assert store.claim_pending_run(first, now=1_000, lease_seconds=120) is not None
    assert _record(store, task, "b", coalesce=True) == "recorded"
    assert _record(store, task, "c", coalesce=True) == "coalesced"
    [second] = store.list_pending_runs()
    assert [e["session_id"] for e in second.event["events"]] == ["b", "c"]
