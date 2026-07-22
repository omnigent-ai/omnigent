"""Tests for :class:`SqlAlchemyScheduledTaskStore`.

Exercises ``create``, ``get``, ``list``, ``list_active``, ``update``,
``delete`` and the run methods (``create_run`` / ``list_runs``) against a real
SQLite database.
"""

from __future__ import annotations

import uuid

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore


# scheduled_tasks.id / scheduled_task_runs.id / scheduled_task_id are Uuid16
# columns (16 raw bytes), read back as bare 32-char hex strings. ``_uid`` maps a
# readable seed to a deterministic bare-hex UUID so tests stay legible while the
# store still round-trips real UUIDs. agent_id / user_id / conversation_id
# stay plain strings — those columns are still ``String``.
def _uid(seed: str) -> str:
    """Deterministic bare 32-char hex UUID string from a short readable seed."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyScheduledTaskStore:
    """A fresh :class:`SqlAlchemyScheduledTaskStore` backed by the test SQLite DB.

    :param db_uri: Per-test SQLite URI from the root conftest fixture.
    :returns: A ready-to-use :class:`SqlAlchemyScheduledTaskStore` instance.
    """
    return SqlAlchemyScheduledTaskStore(db_uri)


# ── create / get ────────────────────────────────────────────────────────────


def test_create_returns_scheduled_task_with_all_fields(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """``create`` echoes every field back, round-tripping the JSON columns."""
    task = store.create(
        scheduled_task_id=_uid("st_1"),
        name="nightly triage",
        prompt="Triage the inbox",
        rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        user_id="alice@example.com",
        agent_id=_uid("ag_abc"),
        timezone="America/Los_Angeles",
        model_override="claude-opus-4-7",
        reasoning_effort="high",
        workspace="/home/alice/repo",
        host_id=_uid("host_abc123"),
    )
    assert task.id == _uid("st_1")
    assert task.workspace_id == 0
    assert task.name == "nightly triage"
    assert task.prompt == "Triage the inbox"
    assert task.rrule == "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"
    assert task.user_id == "alice@example.com"
    assert task.agent_id == _uid("ag_abc")
    assert task.timezone == "America/Los_Angeles"
    assert task.model_override == "claude-opus-4-7"
    assert task.reasoning_effort == "high"
    assert task.workspace == "/home/alice/repo"
    assert task.base_branch is None
    assert task.execution_target == "connected_host"
    assert task.host_id == _uid("host_abc123")
    assert task.state == "active"
    assert task.last_run_at is None
    assert task.last_run_conversation_id is None
    assert task.created_at > 0
    assert task.updated_at is None


def test_create_minimal_defaults(store: SqlAlchemyScheduledTaskStore) -> None:
    """Optional fields default sensibly (None overrides)."""
    task = store.create(
        scheduled_task_id=_uid("st_min"),
        name="minimal",
        prompt="do a thing",
        rrule="FREQ=MINUTELY",
        user_id="bob@example.com",
        agent_id=_uid("ag_min"),
        timezone="UTC",
    )
    assert task.model_override is None
    assert task.reasoning_effort is None
    assert task.workspace is None
    assert task.base_branch is None
    assert task.execution_target == "connected_host"
    assert task.host_id is None
    assert task.state == "active"


# ── state enum ────────────────────────────────────────────────────────────────


def test_state_round_trips_as_string(store: SqlAlchemyScheduledTaskStore) -> None:
    """Every valid state name survives the string→int→string round trip.

    The entity exposes ``state`` as a string; the column stores an int code.
    """
    for i, name in enumerate(("active", "paused", "deleted")):
        task = store.create(
            scheduled_task_id=_uid(f"st_state_{i}"),
            name="n",
            prompt="p",
            rrule="FREQ=MINUTELY",
            user_id="u",
            agent_id=_uid("ag"),
            timezone="UTC",
            state=name,
        )
        assert task.state == name
        assert isinstance(task.state, str)


def test_create_rejects_invalid_state(store: SqlAlchemyScheduledTaskStore) -> None:
    """An unknown state name is rejected by the codec (never reaches the DB)."""
    with pytest.raises(ValueError, match=r"scheduled_tasks\.state"):
        store.create(
            scheduled_task_id=_uid("st_badstate"),
            name="n",
            prompt="p",
            rrule="FREQ=MINUTELY",
            user_id="u",
            agent_id=_uid("ag"),
            timezone="UTC",
            state="bogus",
        )


def test_update_host_id_reads_back(store: SqlAlchemyScheduledTaskStore) -> None:
    """Updating ``host_id`` reads the new value back."""
    store.create(
        scheduled_task_id=_uid("st_upd_host"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    updated = store.update(_uid("st_upd_host"), host_id=_uid("host_xyz"))
    assert updated is not None
    assert updated.execution_target == "connected_host"
    assert updated.host_id == _uid("host_xyz")


def test_update_state_reads_back(store: SqlAlchemyScheduledTaskStore) -> None:
    """Updating ``state`` to ``paused`` reads back ``paused``."""
    store.create(
        scheduled_task_id=_uid("st_upd_state"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    updated = store.update(_uid("st_upd_state"), state="paused")
    assert updated is not None
    assert updated.state == "paused"


# ── recurring trigger (rrule) ─────────────────────────────────────────────────


def test_create_recurring_task(store: SqlAlchemyScheduledTaskStore) -> None:
    """A recurring task sets ``rrule``."""
    task = store.create(
        scheduled_task_id=_uid("st_recur"),
        name="recurring",
        prompt="p",
        rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    assert task.rrule == "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"


def test_update_changes_rrule(store: SqlAlchemyScheduledTaskStore) -> None:
    """Updating with a new ``rrule`` reschedules the recurring trigger."""
    store.create(
        scheduled_task_id=_uid("st_recron"),
        name="n",
        prompt="p",
        rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    updated = store.update(_uid("st_recron"), rrule="FREQ=DAILY;BYHOUR=0;BYMINUTE=0")
    assert updated is not None
    assert updated.rrule == "FREQ=DAILY;BYHOUR=0;BYMINUTE=0"


def test_get_returns_created_task(store: SqlAlchemyScheduledTaskStore) -> None:
    """``get`` returns a previously created task."""
    store.create(
        scheduled_task_id=_uid("st_get"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag_1"),
        timezone="UTC",
    )
    fetched = store.get(_uid("st_get"))
    assert fetched is not None
    assert fetched.id == _uid("st_get")


def test_get_missing_returns_none(store: SqlAlchemyScheduledTaskStore) -> None:
    """``get`` returns ``None`` for an unknown id."""
    assert store.get(_uid("st_nope")) is None


# ── list / list_active ────────────────────────────────────────────────────────


def test_list_orders_by_created_at_then_id(store: SqlAlchemyScheduledTaskStore) -> None:
    """``list`` returns all tasks ordered by ``created_at, id``.

    ``created_at`` is integer seconds, so two back-to-back creates tie on it and
    the tiebreak is ``id`` ASC. ``Uuid16`` orders by raw bytes, so pick two ids
    whose byte order is deterministic (``…aa`` < ``…bb``). Ids are bare 32-char
    hex, matching what the store reads back.
    """
    id_a = "000000000000000000000000000000aa"
    id_b = "000000000000000000000000000000bb"
    store.create(
        scheduled_task_id=id_a,
        name="a",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    store.create(
        scheduled_task_id=id_b,
        name="b",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    ids = [r.id for r in store.list()]
    assert ids == [id_a, id_b]


def test_list_active_excludes_non_active(store: SqlAlchemyScheduledTaskStore) -> None:
    """``list_active`` returns only active tasks, excluding paused/deleted."""
    store.create(
        scheduled_task_id=_uid("st_active"),
        name="active",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
        state="active",
    )
    for i, other_state in enumerate(("paused", "deleted")):
        store.create(
            scheduled_task_id=_uid(f"st_{other_state}"),
            name=other_state,
            prompt="p",
            rrule="FREQ=MINUTELY",
            user_id="u",
            agent_id=_uid("ag"),
            timezone="UTC",
            state=other_state,
        )
    active_ids = [r.id for r in store.list_active()]
    assert active_ids == [_uid("st_active")]


def test_list_active_all_workspaces_includes_tenant_tasks(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """Scheduler startup can discover active tasks outside ambient workspace 0."""
    with workspace_scope(42):
        task_42 = store.create(
            scheduled_task_id=_uid("st_active_ws42"),
            name="tenant",
            prompt="p",
            rrule="FREQ=MINUTELY",
            user_id="u",
            agent_id=_uid("ag"),
            timezone="UTC",
        )
    with workspace_scope(7):
        store.create(
            scheduled_task_id=_uid("st_paused_ws7"),
            name="paused",
            prompt="p",
            rrule="FREQ=MINUTELY",
            user_id="u",
            agent_id=_uid("ag"),
            timezone="UTC",
            state="paused",
        )

    tasks = store.list_active_all_workspaces()

    assert [(t.workspace_id, t.id) for t in tasks] == [(42, task_42.id)]


def test_list_active_all_workspaces_pages_beyond_batch_size(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """The scheduler-boot scan returns EVERY active task by keyset-paging
    internally — no silent cap that would leave tasks beyond one batch un-armed."""
    store._active_boot_batch_size = 5  # force multiple pages
    total = 17
    created_ids: list[str] = []
    for i in range(total):
        task = store.create(
            scheduled_task_id=_uid(f"st_boot_{i:03d}"),
            name=f"n{i}",
            prompt="p",
            rrule="FREQ=MINUTELY",
            user_id="u",
            agent_id=_uid("ag"),
            timezone="UTC",
        )
        created_ids.append(task.id)

    tasks = store.list_active_all_workspaces()
    returned_ids = [t.id for t in tasks]

    assert len(returned_ids) == total
    assert set(returned_ids) == set(created_ids)  # no gaps
    assert len(returned_ids) == len(set(returned_ids))  # no dupes


# ── update ────────────────────────────────────────────────────────────────────


def test_update_changes_fields_and_stamps_updated_at(store: SqlAlchemyScheduledTaskStore) -> None:
    """``update`` mutates supplied fields and sets ``updated_at``."""
    store.create(
        scheduled_task_id=_uid("st_u"),
        name="before",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    updated = store.update(
        _uid("st_u"),
        name="after",
        rrule="FREQ=DAILY;BYHOUR=0;BYMINUTE=0",
        state="paused",
        last_run_at=1700000000,
        last_run_conversation_id=_uid("conv_x"),
    )
    assert updated is not None
    assert updated.name == "after"
    assert updated.rrule == "FREQ=DAILY;BYHOUR=0;BYMINUTE=0"
    assert updated.base_branch is None
    assert updated.state == "paused"
    assert updated.last_run_at == 1700000000
    assert updated.last_run_conversation_id == _uid("conv_x")
    assert updated.updated_at is not None


def test_update_noop_leaves_updated_at_none(store: SqlAlchemyScheduledTaskStore) -> None:
    """An update that changes nothing does not stamp ``updated_at``."""
    store.create(
        scheduled_task_id=_uid("st_noop"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    result = store.update(_uid("st_noop"), name="n")  # same value
    assert result is not None
    assert result.updated_at is None


def test_update_missing_returns_none(store: SqlAlchemyScheduledTaskStore) -> None:
    """Updating an unknown task returns ``None``."""
    assert store.update(_uid("st_missing"), name="x") is None


# ── delete ────────────────────────────────────────────────────────────────────


def test_delete_removes_task(store: SqlAlchemyScheduledTaskStore) -> None:
    """``delete`` removes the row and returns ``True``."""
    store.create(
        scheduled_task_id=_uid("st_del"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    assert store.delete(_uid("st_del")) is True
    assert store.get(_uid("st_del")) is None


def test_delete_missing_returns_false(store: SqlAlchemyScheduledTaskStore) -> None:
    """``delete`` is idempotent — returns ``False`` when nothing was removed."""
    assert store.delete(_uid("st_missing")) is False


# ── runs ──────────────────────────────────────────────────────────────────────


def test_create_run_and_list_runs(store: SqlAlchemyScheduledTaskStore) -> None:
    """Runs are created and listed most-recent-first."""
    store.create(
        scheduled_task_id=_uid("st_runs"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    store.create_run(
        run_id=_uid("sr_1"),
        scheduled_task_id=_uid("st_runs"),
        status="succeeded",
        scheduled_at=100,
        conversation_id=_uid("conv_1"),
        fired_at=101,
        finished_at=102,
    )
    store.create_run(
        run_id=_uid("sr_2"),
        scheduled_task_id=_uid("st_runs"),
        status="failed",
        scheduled_at=200,
        error="boom",
        error_code="rate_limited",
    )
    runs, next_cursor = store.list_runs(_uid("st_runs"))
    assert next_cursor is None
    assert [r.id for r in runs] == [_uid("sr_2"), _uid("sr_1")]  # scheduled_at DESC
    assert runs[0].status == "failed"
    assert runs[0].error == "boom"
    assert runs[0].error_code == "rate_limited"
    assert runs[1].error_code is None
    assert runs[1].conversation_id == _uid("conv_1")
    assert runs[1].fired_at == 101
    assert runs[1].finished_at == 102


def test_list_runs_cursor_pagination(store: SqlAlchemyScheduledTaskStore) -> None:
    """Paging by (limit, after_id) returns every run exactly once, newest-first,
    with a null cursor on the last page — no gaps, no dupes."""
    store.create(
        scheduled_task_id=_uid("st_page"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    total = 25
    expected_newest_first: list[str] = []
    for i in range(total):
        rid = _uid(f"sr_page_{i:03d}")
        store.create_run(
            run_id=rid,
            scheduled_task_id=_uid("st_page"),
            status="succeeded",
            scheduled_at=1000 + i,
        )
        expected_newest_first.insert(0, rid)

    collected: list[str] = []
    after: str | None = None
    pages = 0
    while True:
        runs, after = store.list_runs(_uid("st_page"), limit=10, after_id=after)
        collected.extend(r.id for r in runs)
        pages += 1
        if after is None:
            break
        assert pages < 10, "pagination did not terminate"

    assert collected == expected_newest_first
    # 25 rows / page 10 -> 3 pages (10, 10, 5), last page has null cursor.
    assert pages == 3


def test_list_runs_cursor_pagination_ties_on_scheduled_at(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """When many runs share a scheduled_at, the (scheduled_at, id) keyset still
    walks every row once (id tiebreak), proving an id-only cursor would be wrong."""
    store.create(
        scheduled_task_id=_uid("st_ties"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    ids = [_uid(f"sr_tie_{i:03d}") for i in range(15)]
    for rid in ids:
        store.create_run(
            run_id=rid,
            scheduled_task_id=_uid("st_ties"),
            status="succeeded",
            scheduled_at=500,
        )
    expected = sorted(ids, reverse=True)  # id DESC when scheduled_at is equal

    collected: list[str] = []
    after: str | None = None
    while True:
        runs, after = store.list_runs(_uid("st_ties"), limit=4, after_id=after)
        collected.extend(r.id for r in runs)
        if after is None:
            break
    assert collected == expected


def test_list_runs_scoped_to_task(store: SqlAlchemyScheduledTaskStore) -> None:
    """``list_runs`` only returns runs for the requested task."""
    for rid in ("st_x", "st_y"):
        store.create(
            scheduled_task_id=_uid(rid),
            name=rid,
            prompt="p",
            rrule="FREQ=MINUTELY",
            user_id="u",
            agent_id=_uid("ag"),
            timezone="UTC",
        )
    store.create_run(
        run_id=_uid("sr_x"), scheduled_task_id=_uid("st_x"), status="scheduled", scheduled_at=1
    )
    store.create_run(
        run_id=_uid("sr_y"), scheduled_task_id=_uid("st_y"), status="scheduled", scheduled_at=1
    )
    assert [r.id for r in store.list_runs(_uid("st_x"))[0]] == [_uid("sr_x")]


def test_list_runs_empty_for_unknown_task(store: SqlAlchemyScheduledTaskStore) -> None:
    """A task with no runs (or an unknown id) yields an empty list and no cursor."""
    assert store.list_runs(_uid("st_none")) == ([], None)


def test_run_status_round_trips_as_string(store: SqlAlchemyScheduledTaskStore) -> None:
    """Every valid status name survives the string→int→string round trip.

    The entity exposes ``status`` as a string; the column stores an int code.
    The store translates at the boundary, so what goes in comes back out
    unchanged for every member of the closed set.
    """
    store.create(
        scheduled_task_id=_uid("st_rt"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    for i, name in enumerate(("scheduled", "running", "succeeded", "failed", "skipped")):
        run = store.create_run(
            run_id=_uid(f"sr_{i}"),
            scheduled_task_id=_uid("st_rt"),
            status=name,
            scheduled_at=i,
        )
        assert run.status == name
        assert isinstance(run.status, str)


def test_create_run_rejects_invalid_status_name(store: SqlAlchemyScheduledTaskStore) -> None:
    """An unknown status name is rejected by the codec (never reaches the DB)."""
    store.create(
        scheduled_task_id=_uid("st_bad"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    with pytest.raises(ValueError, match=r"scheduled_task_runs\.status"):
        store.create_run(
            run_id=_uid("sr_bad"),
            scheduled_task_id=_uid("st_bad"),
            status="bogus",
            scheduled_at=1,
        )


# ── update: sentinel / NULL-clear behaviour (Finding 1) ──────────────────────


def test_update_host_id_can_be_cleared_to_null(store: SqlAlchemyScheduledTaskStore) -> None:
    """Passing ``host_id=None`` explicitly clears the column to NULL (set→NULL)."""
    store.create(
        scheduled_task_id=_uid("st_clear_host"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
        host_id=_uid("host_abc"),
    )
    updated = store.update(_uid("st_clear_host"), host_id=None)
    assert updated is not None
    assert updated.execution_target == "connected_host"
    assert updated.host_id is None
    fetched = store.get(_uid("st_clear_host"))
    assert fetched is not None
    assert fetched.host_id is None


def test_update_last_run_conversation_id_can_be_cleared_to_null(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """Passing ``last_run_conversation_id=None`` explicitly nulls the column."""
    store.create(
        scheduled_task_id=_uid("st_clear_conv"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    store.update(_uid("st_clear_conv"), last_run_conversation_id=_uid("conv_abc"))
    updated = store.update(_uid("st_clear_conv"), last_run_conversation_id=None)
    assert updated is not None
    assert updated.last_run_conversation_id is None
    fetched = store.get(_uid("st_clear_conv"))
    assert fetched is not None
    assert fetched.last_run_conversation_id is None


def test_update_omitting_nullable_param_leaves_field_unchanged(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """Omitting ``host_id`` / ``last_run_conversation_id`` does NOT change them."""
    store.create(
        scheduled_task_id=_uid("st_omit"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
        host_id=_uid("host_keep"),
    )
    store.update(_uid("st_omit"), last_run_conversation_id=_uid("conv_keep"))
    # Update name only — host_id and last_run_conversation_id must be untouched.
    updated = store.update(_uid("st_omit"), name="new_name")
    assert updated is not None
    assert updated.host_id == _uid("host_keep")
    assert updated.last_run_conversation_id == _uid("conv_keep")


def test_update_clearing_already_null_field_is_noop_for_updated_at(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """Clearing a field that is already NULL does not stamp ``updated_at``."""
    store.create(
        scheduled_task_id=_uid("st_null_noop"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    # host_id starts NULL; passing None should be a no-op (no updated_at).
    result = store.update(_uid("st_null_noop"), host_id=None)
    assert result is not None
    assert result.updated_at is None


# ── delete: cascade cleanup of runs (Finding 2) ──────────────────────────────


def test_delete_also_removes_associated_runs(store: SqlAlchemyScheduledTaskStore) -> None:
    """Deleting a task removes all of its runs."""
    store.create(
        scheduled_task_id=_uid("st_del_runs"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    store.create_run(
        run_id=_uid("sr_del_1"),
        scheduled_task_id=_uid("st_del_runs"),
        status="succeeded",
        scheduled_at=1,
    )
    store.create_run(
        run_id=_uid("sr_del_2"),
        scheduled_task_id=_uid("st_del_runs"),
        status="failed",
        scheduled_at=2,
    )
    assert len(store.list_runs(_uid("st_del_runs"))[0]) == 2
    store.delete(_uid("st_del_runs"))
    assert store.list_runs(_uid("st_del_runs")) == ([], None)


def test_delete_does_not_remove_other_tasks_runs(store: SqlAlchemyScheduledTaskStore) -> None:
    """Deleting task A must not affect task B's runs."""
    for tid in ("st_a_scope", "st_b_scope"):
        store.create(
            scheduled_task_id=_uid(tid),
            name=tid,
            prompt="p",
            rrule="FREQ=MINUTELY",
            user_id="u",
            agent_id=_uid("ag"),
            timezone="UTC",
        )
        store.create_run(
            run_id=_uid(f"sr_{tid}"),
            scheduled_task_id=_uid(tid),
            status="scheduled",
            scheduled_at=1,
        )
    store.delete(_uid("st_a_scope"))
    assert store.list_runs(_uid("st_a_scope")) == ([], None)
    remaining, _ = store.list_runs(_uid("st_b_scope"))
    assert len(remaining) == 1
    assert remaining[0].id == _uid("sr_st_b_scope")


# ── update_run (terminal transition + idempotency) ───────────────────────────


def _seed_running_run(store: SqlAlchemyScheduledTaskStore, seed: str) -> str:
    """Create a task + a ``running`` run for it; return the run id."""
    store.create(
        scheduled_task_id=_uid(f"task_{seed}"),
        name=seed,
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    run_id = _uid(f"run_{seed}")
    store.create_run(
        run_id=run_id,
        scheduled_task_id=_uid(f"task_{seed}"),
        status="running",
        scheduled_at=100,
        conversation_id=_uid(f"conv_{seed}"),
        fired_at=101,
    )
    return run_id


def test_update_run_transitions_running_to_succeeded(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """``update_run`` flips a ``running`` run to ``succeeded`` with finished_at."""
    run_id = _seed_running_run(store, "ok")
    updated = store.update_run(run_id, status="succeeded", finished_at=202)
    assert updated is not None
    assert updated.status == "succeeded"
    assert updated.finished_at == 202
    assert updated.error is None and updated.error_code is None


def test_update_run_transitions_running_to_failed_with_code(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """``update_run`` flips a ``running`` run to ``failed`` carrying error detail."""
    run_id = _seed_running_run(store, "bad")
    updated = store.update_run(
        run_id, status="failed", finished_at=303, error="boom", error_code="incomplete"
    )
    assert updated is not None
    assert updated.status == "failed"
    assert updated.finished_at == 303
    assert updated.error == "boom"
    assert updated.error_code == "incomplete"


def test_update_run_is_idempotent_on_already_terminal(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """A second ``update_run`` on an already-terminal run is a no-op (returns None).

    The conditional ``WHERE status = running`` guard means a run advanced to a
    terminal state — by a prior sweep or a fire-time write — is never
    clobbered, and two concurrent sweeps cannot double-transition it.
    """
    run_id = _seed_running_run(store, "once")
    first = store.update_run(run_id, status="succeeded", finished_at=202)
    assert first is not None and first.status == "succeeded"
    # Second attempt (e.g. a racing sweep) must not overwrite it.
    second = store.update_run(run_id, status="failed", finished_at=999, error_code="incomplete")
    assert second is None
    # State is unchanged from the first transition.
    run = store.list_runs(_uid("task_once"))[0][0]
    assert run.status == "succeeded"
    assert run.finished_at == 202
    assert run.error_code is None


def test_update_run_unknown_run_returns_none(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """``update_run`` on a missing run id returns ``None``."""
    assert store.update_run(_uid("nope"), status="succeeded", finished_at=1) is None


# ── list_running_runs_for_tasks (lazy-on-read LIST backstop source) ──────────


def test_list_running_runs_for_tasks_filters_status_and_tasks(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """Returns only ``running`` runs, and only for the requested tasks.

    Powers the LIST endpoint's lazy stale backstop: the route passes the
    owner's task ids and gets back their still-``running`` runs to age-check.
    """
    for seed in ("a", "b"):
        store.create(
            scheduled_task_id=_uid(f"task_{seed}"),
            name=seed,
            prompt="p",
            rrule="FREQ=MINUTELY",
            user_id="u",
            agent_id=_uid("ag"),
            timezone="UTC",
        )
    # task_a: one running + one terminal run.
    store.create_run(
        run_id=_uid("run_a_running"),
        scheduled_task_id=_uid("task_a"),
        status="running",
        scheduled_at=100,
    )
    store.create_run(
        run_id=_uid("run_a_done"),
        scheduled_task_id=_uid("task_a"),
        status="succeeded",
        scheduled_at=90,
        finished_at=95,
    )
    # task_b: one running run.
    store.create_run(
        run_id=_uid("run_b_running"),
        scheduled_task_id=_uid("task_b"),
        status="running",
        scheduled_at=200,
    )

    got = store.list_running_runs_for_tasks([_uid("task_a"), _uid("task_b")])
    ids = {r.id for r in got}
    assert ids == {_uid("run_a_running"), _uid("run_b_running")}  # terminal excluded
    # Ordered scheduled_at DESC (run_b scheduled_at=200 > run_a=100).
    assert got[0].id == _uid("run_b_running")


def test_list_running_runs_for_tasks_empty_ids_returns_empty(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """An empty task-id list short-circuits to an empty result (no query)."""
    assert store.list_running_runs_for_tasks([]) == []


def test_list_running_runs_for_tasks_is_workspace_scoped(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """A task's running run is invisible from another workspace."""
    with workspace_scope(11):
        store.create(
            scheduled_task_id=_uid("task_w11"),
            name="w11",
            prompt="p",
            rrule="FREQ=MINUTELY",
            user_id="a",
            agent_id=_uid("ag"),
            timezone="UTC",
        )
        store.create_run(
            run_id=_uid("run_w11"),
            scheduled_task_id=_uid("task_w11"),
            status="running",
            scheduled_at=100,
        )
    # Default workspace cannot see the workspace-11 run.
    assert store.list_running_runs_for_tasks([_uid("task_w11")]) == []
    with workspace_scope(11):
        got = store.list_running_runs_for_tasks([_uid("task_w11")])
        assert [r.id for r in got] == [_uid("run_w11")]


# ── get_running_run_by_conversation (event-hook reverse lookup) ───────────────


def test_get_running_run_by_conversation_returns_running_run(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """The reverse lookup finds the ``running`` run for a conversation."""
    run_id = _seed_running_run(store, "hook")
    found = store.get_running_run_by_conversation(_uid("conv_hook"))
    assert found is not None
    assert found.id == run_id
    assert found.status == "running"


def test_get_running_run_by_conversation_none_when_terminal(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """Once the run is terminal the reverse lookup returns ``None`` (hook no-op).

    This is what makes the event hook idempotent: a second terminal edge finds
    no ``running`` run to transition.
    """
    run_id = _seed_running_run(store, "term")
    store.update_run(run_id, status="succeeded", finished_at=202)
    assert store.get_running_run_by_conversation(_uid("conv_term")) is None


def test_get_running_run_by_conversation_none_for_unknown_conversation(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """An interactive (non-scheduled) conversation has no run → ``None``."""
    assert store.get_running_run_by_conversation(_uid("conv_absent")) is None


def test_get_running_run_by_conversation_is_workspace_scoped(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """The lookup filters on the current workspace, like every other store read.

    A run seeded in workspace 11 is invisible from the default workspace and
    visible only inside its own ``workspace_scope`` — the property the event
    hook relies on to write to the fired run's workspace.
    """
    with workspace_scope(11):
        store.create(
            scheduled_task_id=_uid("task_ws"),
            name="ws",
            prompt="p",
            rrule="FREQ=MINUTELY",
            user_id="a",
            agent_id=_uid("ag"),
            timezone="UTC",
        )
        store.create_run(
            run_id=_uid("run_ws"),
            scheduled_task_id=_uid("task_ws"),
            status="running",
            scheduled_at=100,
            conversation_id=_uid("conv_ws"),
        )
    # Default workspace cannot see the workspace-11 run.
    assert store.get_running_run_by_conversation(_uid("conv_ws")) is None
    # Inside its own scope it resolves.
    with workspace_scope(11):
        found = store.get_running_run_by_conversation(_uid("conv_ws"))
        assert found is not None and found.id == _uid("run_ws")
