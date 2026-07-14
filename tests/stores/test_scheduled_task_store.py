"""Tests for :class:`SqlAlchemyScheduledTaskStore`.

Exercises ``create``, ``get``, ``list``, ``list_active``, ``update``,
``delete`` and the run methods (``create_run`` / ``list_runs``) against a real
SQLite database.
"""

from __future__ import annotations

import pytest

from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore


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
        scheduled_task_id="st_1",
        name="nightly triage",
        prompt="Triage the inbox",
        cron_expression="0 9 * * *",
        owner_user_id="alice@example.com",
        agent_id="ag_abc",
        timezone="America/Los_Angeles",
        model_override="claude-opus-4-7",
        reasoning_effort="high",
        workspace="/home/alice/repo",
        base_branch="main",
    )
    assert task.id == "st_1"
    assert task.name == "nightly triage"
    assert task.prompt == "Triage the inbox"
    assert task.cron_expression == "0 9 * * *"
    assert task.owner_user_id == "alice@example.com"
    assert task.agent_id == "ag_abc"
    assert task.timezone == "America/Los_Angeles"
    assert task.model_override == "claude-opus-4-7"
    assert task.reasoning_effort == "high"
    assert task.workspace == "/home/alice/repo"
    assert task.base_branch == "main"
    assert task.state == "active"
    assert task.last_run_at is None
    assert task.last_run_conversation_id is None
    assert task.created_at > 0
    assert task.updated_at is None


def test_create_minimal_defaults(store: SqlAlchemyScheduledTaskStore) -> None:
    """Optional fields default sensibly (None overrides)."""
    task = store.create(
        scheduled_task_id="st_min",
        name="minimal",
        prompt="do a thing",
        cron_expression="* * * * *",
        owner_user_id="bob@example.com",
        agent_id="ag_min",
        timezone="UTC",
    )
    assert task.model_override is None
    assert task.reasoning_effort is None
    assert task.workspace is None
    assert task.base_branch is None
    assert task.state == "active"


# ── state enum ────────────────────────────────────────────────────────────────


def test_state_round_trips_as_string(store: SqlAlchemyScheduledTaskStore) -> None:
    """Every valid state name survives the string→int→string round trip.

    The entity exposes ``state`` as a string; the column stores an int code.
    """
    for i, name in enumerate(("active", "paused", "deleted")):
        task = store.create(
            scheduled_task_id=f"st_state_{i}",
            name="n",
            prompt="p",
            cron_expression="* * * * *",
            owner_user_id="u",
            agent_id="ag",
            timezone="UTC",
            state=name,
        )
        assert task.state == name
        assert isinstance(task.state, str)


def test_create_rejects_invalid_state(store: SqlAlchemyScheduledTaskStore) -> None:
    """An unknown state name is rejected by the codec (never reaches the DB)."""
    with pytest.raises(ValueError, match=r"scheduled_tasks\.state"):
        store.create(
            scheduled_task_id="st_badstate",
            name="n",
            prompt="p",
            cron_expression="* * * * *",
            owner_user_id="u",
            agent_id="ag",
            timezone="UTC",
            state="bogus",
        )


def test_update_state_reads_back(store: SqlAlchemyScheduledTaskStore) -> None:
    """Updating ``state`` to ``paused`` reads back ``paused``."""
    store.create(
        scheduled_task_id="st_upd_state",
        name="n",
        prompt="p",
        cron_expression="* * * * *",
        owner_user_id="u",
        agent_id="ag",
        timezone="UTC",
    )
    updated = store.update("st_upd_state", state="paused")
    assert updated is not None
    assert updated.state == "paused"


# ── recurring trigger (cron_expression) ───────────────────────────────────────


def test_create_recurring_task(store: SqlAlchemyScheduledTaskStore) -> None:
    """A recurring task sets ``cron_expression``."""
    task = store.create(
        scheduled_task_id="st_recur",
        name="recurring",
        prompt="p",
        cron_expression="0 9 * * *",
        owner_user_id="u",
        agent_id="ag",
        timezone="UTC",
    )
    assert task.cron_expression == "0 9 * * *"


def test_update_changes_cron_expression(store: SqlAlchemyScheduledTaskStore) -> None:
    """Updating with a new ``cron_expression`` reschedules the recurring trigger."""
    store.create(
        scheduled_task_id="st_recron",
        name="n",
        prompt="p",
        cron_expression="0 9 * * *",
        owner_user_id="u",
        agent_id="ag",
        timezone="UTC",
    )
    updated = store.update("st_recron", cron_expression="0 0 * * *")
    assert updated is not None
    assert updated.cron_expression == "0 0 * * *"


def test_get_returns_created_task(store: SqlAlchemyScheduledTaskStore) -> None:
    """``get`` returns a previously created task."""
    store.create(
        scheduled_task_id="st_get",
        name="n",
        prompt="p",
        cron_expression="* * * * *",
        owner_user_id="u",
        agent_id="ag_1",
        timezone="UTC",
    )
    fetched = store.get("st_get")
    assert fetched is not None
    assert fetched.id == "st_get"


def test_get_missing_returns_none(store: SqlAlchemyScheduledTaskStore) -> None:
    """``get`` returns ``None`` for an unknown id."""
    assert store.get("st_nope") is None


# ── list / list_active ────────────────────────────────────────────────────────


def test_list_orders_by_created_at_then_id(store: SqlAlchemyScheduledTaskStore) -> None:
    """``list`` returns all tasks ordered by ``created_at, id``."""
    store.create(
        scheduled_task_id="st_a",
        name="a",
        prompt="p",
        cron_expression="* * * * *",
        owner_user_id="u",
        agent_id="ag",
        timezone="UTC",
    )
    store.create(
        scheduled_task_id="st_b",
        name="b",
        prompt="p",
        cron_expression="* * * * *",
        owner_user_id="u",
        agent_id="ag",
        timezone="UTC",
    )
    ids = [r.id for r in store.list()]
    assert ids == ["st_a", "st_b"]


def test_list_active_excludes_non_active(store: SqlAlchemyScheduledTaskStore) -> None:
    """``list_active`` returns only active tasks, excluding paused/deleted."""
    store.create(
        scheduled_task_id="st_active",
        name="active",
        prompt="p",
        cron_expression="* * * * *",
        owner_user_id="u",
        agent_id="ag",
        timezone="UTC",
        state="active",
    )
    for i, other_state in enumerate(("paused", "deleted")):
        store.create(
            scheduled_task_id=f"st_{other_state}",
            name=other_state,
            prompt="p",
            cron_expression="* * * * *",
            owner_user_id="u",
            agent_id="ag",
            timezone="UTC",
            state=other_state,
        )
    active_ids = [r.id for r in store.list_active()]
    assert active_ids == ["st_active"]


# ── update ────────────────────────────────────────────────────────────────────


def test_update_changes_fields_and_stamps_updated_at(store: SqlAlchemyScheduledTaskStore) -> None:
    """``update`` mutates supplied fields and sets ``updated_at``."""
    store.create(
        scheduled_task_id="st_u",
        name="before",
        prompt="p",
        cron_expression="* * * * *",
        owner_user_id="u",
        agent_id="ag",
        timezone="UTC",
    )
    updated = store.update(
        "st_u",
        name="after",
        cron_expression="0 0 * * *",
        base_branch="develop",
        state="paused",
        last_run_at=1700000000,
        last_run_conversation_id="conv_x",
    )
    assert updated is not None
    assert updated.name == "after"
    assert updated.cron_expression == "0 0 * * *"
    assert updated.base_branch == "develop"
    assert updated.state == "paused"
    assert updated.last_run_at == 1700000000
    assert updated.last_run_conversation_id == "conv_x"
    assert updated.updated_at is not None


def test_update_noop_leaves_updated_at_none(store: SqlAlchemyScheduledTaskStore) -> None:
    """An update that changes nothing does not stamp ``updated_at``."""
    store.create(
        scheduled_task_id="st_noop",
        name="n",
        prompt="p",
        cron_expression="* * * * *",
        owner_user_id="u",
        agent_id="ag",
        timezone="UTC",
    )
    result = store.update("st_noop", name="n")  # same value
    assert result is not None
    assert result.updated_at is None


def test_update_missing_returns_none(store: SqlAlchemyScheduledTaskStore) -> None:
    """Updating an unknown task returns ``None``."""
    assert store.update("st_missing", name="x") is None


# ── delete ────────────────────────────────────────────────────────────────────


def test_delete_removes_task(store: SqlAlchemyScheduledTaskStore) -> None:
    """``delete`` removes the row and returns ``True``."""
    store.create(
        scheduled_task_id="st_del",
        name="n",
        prompt="p",
        cron_expression="* * * * *",
        owner_user_id="u",
        agent_id="ag",
        timezone="UTC",
    )
    assert store.delete("st_del") is True
    assert store.get("st_del") is None


def test_delete_missing_returns_false(store: SqlAlchemyScheduledTaskStore) -> None:
    """``delete`` is idempotent — returns ``False`` when nothing was removed."""
    assert store.delete("st_missing") is False


# ── runs ──────────────────────────────────────────────────────────────────────


def test_create_run_and_list_runs(store: SqlAlchemyScheduledTaskStore) -> None:
    """Runs are created and listed most-recent-first."""
    store.create(
        scheduled_task_id="st_runs",
        name="n",
        prompt="p",
        cron_expression="* * * * *",
        owner_user_id="u",
        agent_id="ag",
        timezone="UTC",
    )
    store.create_run(
        run_id="sr_1",
        scheduled_task_id="st_runs",
        status="succeeded",
        scheduled_at=100,
        conversation_id="conv_1",
        fired_at=101,
        finished_at=102,
    )
    store.create_run(
        run_id="sr_2",
        scheduled_task_id="st_runs",
        status="failed",
        scheduled_at=200,
        error="boom",
        error_code="rate_limited",
    )
    runs = store.list_runs("st_runs")
    assert [r.id for r in runs] == ["sr_2", "sr_1"]  # scheduled_at DESC
    assert runs[0].status == "failed"
    assert runs[0].error == "boom"
    assert runs[0].error_code == "rate_limited"
    assert runs[1].error_code is None
    assert runs[1].conversation_id == "conv_1"
    assert runs[1].fired_at == 101
    assert runs[1].finished_at == 102


def test_list_runs_scoped_to_task(store: SqlAlchemyScheduledTaskStore) -> None:
    """``list_runs`` only returns runs for the requested task."""
    for rid in ("st_x", "st_y"):
        store.create(
            scheduled_task_id=rid,
            name=rid,
            prompt="p",
            cron_expression="* * * * *",
            owner_user_id="u",
            agent_id="ag",
            timezone="UTC",
        )
    store.create_run(run_id="sr_x", scheduled_task_id="st_x", status="scheduled", scheduled_at=1)
    store.create_run(run_id="sr_y", scheduled_task_id="st_y", status="scheduled", scheduled_at=1)
    assert [r.id for r in store.list_runs("st_x")] == ["sr_x"]


def test_list_runs_empty_for_unknown_task(store: SqlAlchemyScheduledTaskStore) -> None:
    """A task with no runs (or an unknown id) yields an empty list."""
    assert store.list_runs("st_none") == []


def test_run_status_round_trips_as_string(store: SqlAlchemyScheduledTaskStore) -> None:
    """Every valid status name survives the string→int→string round trip.

    The entity exposes ``status`` as a string; the column stores an int code.
    The store translates at the boundary, so what goes in comes back out
    unchanged for every member of the closed set.
    """
    store.create(
        scheduled_task_id="st_rt",
        name="n",
        prompt="p",
        cron_expression="* * * * *",
        owner_user_id="u",
        agent_id="ag",
        timezone="UTC",
    )
    for i, name in enumerate(("scheduled", "running", "succeeded", "failed", "skipped")):
        run = store.create_run(
            run_id=f"sr_{i}",
            scheduled_task_id="st_rt",
            status=name,
            scheduled_at=i,
        )
        assert run.status == name
        assert isinstance(run.status, str)


def test_create_run_rejects_invalid_status_name(store: SqlAlchemyScheduledTaskStore) -> None:
    """An unknown status name is rejected by the codec (never reaches the DB)."""
    store.create(
        scheduled_task_id="st_bad",
        name="n",
        prompt="p",
        cron_expression="* * * * *",
        owner_user_id="u",
        agent_id="ag",
        timezone="UTC",
    )
    with pytest.raises(ValueError, match=r"scheduled_task_runs\.status"):
        store.create_run(
            run_id="sr_bad",
            scheduled_task_id="st_bad",
            status="bogus",
            scheduled_at=1,
        )
