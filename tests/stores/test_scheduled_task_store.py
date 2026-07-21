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
    runs = store.list_runs(_uid("st_runs"))
    assert [r.id for r in runs] == [_uid("sr_2"), _uid("sr_1")]  # scheduled_at DESC
    assert runs[0].status == "failed"
    assert runs[0].error == "boom"
    assert runs[0].error_code == "rate_limited"
    assert runs[1].error_code is None
    assert runs[1].conversation_id == _uid("conv_1")
    assert runs[1].fired_at == 101
    assert runs[1].finished_at == 102


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
    assert [r.id for r in store.list_runs(_uid("st_x"))] == [_uid("sr_x")]


def test_list_runs_empty_for_unknown_task(store: SqlAlchemyScheduledTaskStore) -> None:
    """A task with no runs (or an unknown id) yields an empty list."""
    assert store.list_runs(_uid("st_none")) == []


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
    assert len(store.list_runs(_uid("st_del_runs"))) == 2
    store.delete(_uid("st_del_runs"))
    assert store.list_runs(_uid("st_del_runs")) == []


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
    assert store.list_runs(_uid("st_a_scope")) == []
    remaining = store.list_runs(_uid("st_b_scope"))
    assert len(remaining) == 1
    assert remaining[0].id == _uid("sr_st_b_scope")
