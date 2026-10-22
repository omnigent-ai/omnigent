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
        permission_mode="acceptEdits",
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
    assert task.permission_mode == "acceptEdits"
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
    assert task.permission_mode is None
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


def test_execution_target_managed_sandbox_round_trips(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """A managed_sandbox task persists and reads back, and update can switch it."""
    created = store.create(
        scheduled_task_id=_uid("st_managed"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
        execution_target="managed_sandbox",
    )
    assert created.execution_target == "managed_sandbox"
    assert store.get(_uid("st_managed")).execution_target == "managed_sandbox"
    # Switch back to connected_host via update.
    switched = store.update(_uid("st_managed"), execution_target="connected_host")
    assert switched is not None
    assert switched.execution_target == "connected_host"


def test_update_workspace_explicit_null_clears_it(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """``update(workspace=None)`` clears the workspace; omitting it leaves it.

    Regression for the managed-switch round-trip: clearing a pinned workspace
    must be possible so switching a task to managed execution (which unpins both
    host and workspace) doesn't strand a workspace-without-host.
    """
    store.create(
        scheduled_task_id=_uid("st_ws"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
        workspace="/repo",
        host_id=_uid("h"),
    )
    # Omitting workspace leaves it unchanged.
    unchanged = store.update(_uid("st_ws"), name="renamed")
    assert unchanged is not None and unchanged.workspace == "/repo"
    # Explicit None clears both host and workspace (the managed-switch shape).
    cleared = store.update(_uid("st_ws"), host_id=None, workspace=None)
    assert cleared is not None
    assert cleared.workspace is None
    assert cleared.host_id is None


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


def test_task_lists_exclude_deleted(store: SqlAlchemyScheduledTaskStore) -> None:
    """General lists keep paused tasks; ``list_active`` returns only active tasks."""
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
    expected_ids = {_uid("st_active"), _uid("st_paused")}
    assert {task.id for task in store.list()} == expected_ids
    assert {task.id for task in store.list(owner_user_id="u")} == expected_ids
    assert store.list(owner_user_id="another-user") == []
    assert store.get(_uid("st_deleted")).state == "deleted"


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


def test_update_rebinds_agent_and_keeps_run_history(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """``update(agent_id=...)`` switches the harness without touching past runs.

    This is what makes an in-place harness switch preferable to recreating the
    task: the run rows stay attached to the same task id.
    """
    store.create(
        scheduled_task_id=_uid("st_rebind"),
        name="n",
        prompt="p",
        rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        user_id="u",
        agent_id=_uid("ag_old"),
        timezone="UTC",
    )
    store.create_run(
        run_id=_uid("sr_old"),
        scheduled_task_id=_uid("st_rebind"),
        status="succeeded",
        scheduled_at=100,
        conversation_id=_uid("conv_old"),
        fired_at=101,
        finished_at=102,
    )

    updated = store.update(_uid("st_rebind"), agent_id=_uid("ag_new"))
    assert updated is not None
    assert updated.agent_id == _uid("ag_new")
    assert updated.updated_at is not None
    reread = store.get(_uid("st_rebind"))
    assert reread is not None and reread.agent_id == _uid("ag_new")

    runs, _ = store.list_runs(_uid("st_rebind"))
    assert [r.id for r in runs] == [_uid("sr_old")]
    # The completed run keeps the conversation it actually ran in.
    assert runs[0].conversation_id == _uid("conv_old")


def test_update_omitting_agent_id_leaves_the_binding_unchanged(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """An ordinary edit must never silently retarget the harness."""
    store.create(
        scheduled_task_id=_uid("st_keepagent"),
        name="n",
        prompt="p",
        rrule="FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        user_id="u",
        agent_id=_uid("ag_keep"),
        timezone="UTC",
    )
    updated = store.update(_uid("st_keepagent"), name="renamed")
    assert updated is not None
    assert updated.agent_id == _uid("ag_keep")


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


def test_update_overrides_can_be_cleared_to_null(store: SqlAlchemyScheduledTaskStore) -> None:
    """Passing ``None`` for an override clears it to NULL (set→agent default).

    Covers the security-relevant case: a task launched with
    ``permission_mode="bypassPermissions"`` must be resettable to the agent
    default through the same edit-then-save path the dialog uses (control reset
    to Default → the API sends an explicit ``null``). Same for model/effort.
    """
    store.create(
        scheduled_task_id=_uid("st_clear_ovr"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
        model_override="opus",
        reasoning_effort="high",
        permission_mode="bypassPermissions",
    )
    updated = store.update(
        _uid("st_clear_ovr"),
        model_override=None,
        reasoning_effort=None,
        permission_mode=None,
    )
    assert updated is not None
    assert updated.model_override is None
    assert updated.reasoning_effort is None
    assert updated.permission_mode is None
    fetched = store.get(_uid("st_clear_ovr"))
    assert fetched is not None
    assert fetched.permission_mode is None


def test_update_omitting_overrides_leaves_them_unchanged(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """Omitting the override params does NOT clear them (sentinel = unchanged)."""
    store.create(
        scheduled_task_id=_uid("st_keep_ovr"),
        name="n",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
        model_override="opus",
        reasoning_effort="high",
        permission_mode="acceptEdits",
    )
    # Update name only — the overrides must be untouched.
    updated = store.update(_uid("st_keep_ovr"), name="renamed")
    assert updated is not None
    assert updated.model_override == "opus"
    assert updated.reasoning_effort == "high"
    assert updated.permission_mode == "acceptEdits"


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


# ── list_latest_run_status_for_tasks (Tasks-list completion badge source) ─────


def test_list_latest_run_status_for_tasks_returns_most_recent_status(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """Returns each task's single most-recent run status by scheduled_at DESC.

    Powers the Tasks-list completion badge in one windowed query. A task with a
    terminal run followed by a newer run reports the NEWER run's status (so a
    run-now firing becomes the badge as soon as it is recorded).
    """
    for seed in ("a", "b"):
        store.create(
            scheduled_task_id=_uid(f"lst_{seed}"),
            name=seed,
            prompt="p",
            rrule="FREQ=MINUTELY",
            user_id="u",
            agent_id=_uid("ag"),
            timezone="UTC",
        )
    # task_a: an older succeeded run, then a newer failed run — failed wins.
    store.create_run(
        run_id=_uid("lst_a_old"),
        scheduled_task_id=_uid("lst_a"),
        status="succeeded",
        scheduled_at=100,
        finished_at=105,
    )
    store.create_run(
        run_id=_uid("lst_a_new"),
        scheduled_task_id=_uid("lst_a"),
        status="failed",
        scheduled_at=200,
        finished_at=205,
    )
    # task_b: a single running run.
    store.create_run(
        run_id=_uid("lst_b_run"),
        scheduled_task_id=_uid("lst_b"),
        status="running",
        scheduled_at=150,
    )

    got = store.list_latest_run_status_for_tasks([_uid("lst_a"), _uid("lst_b")])
    assert got == {_uid("lst_a"): "failed", _uid("lst_b"): "running"}


def test_list_latest_run_status_tiebreaks_on_id_desc(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """Two runs at the SAME scheduled_at break the tie on id DESC.

    Mirrors ``list_runs``' compound ``(scheduled_at DESC, id DESC)`` order so the
    reported status matches the run that heads that task's history.
    """
    store.create(
        scheduled_task_id=_uid("lst_tie"),
        name="tie",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    # Same scheduled_at; the store orders id DESC, so whichever id sorts higher
    # is "latest". Compute that deterministically and assert its status wins.
    low_id, high_id = sorted([_uid("tie_x"), _uid("tie_y")])
    store.create_run(
        run_id=low_id,
        scheduled_task_id=_uid("lst_tie"),
        status="succeeded",
        scheduled_at=300,
        finished_at=305,
    )
    store.create_run(
        run_id=high_id,
        scheduled_task_id=_uid("lst_tie"),
        status="skipped",
        scheduled_at=300,
    )
    got = store.list_latest_run_status_for_tasks([_uid("lst_tie")])
    assert got == {_uid("lst_tie"): "skipped"}


def test_list_latest_run_status_omits_tasks_with_no_runs(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """A task that has never run is absent from the map (badge → never run)."""
    store.create(
        scheduled_task_id=_uid("lst_norun"),
        name="norun",
        prompt="p",
        rrule="FREQ=MINUTELY",
        user_id="u",
        agent_id=_uid("ag"),
        timezone="UTC",
    )
    assert store.list_latest_run_status_for_tasks([_uid("lst_norun")]) == {}


def test_list_latest_run_status_empty_ids_returns_empty(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """An empty task-id list short-circuits to an empty map (no query)."""
    assert store.list_latest_run_status_for_tasks([]) == {}


def test_list_latest_run_status_is_workspace_scoped(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    """A task's runs are invisible to the latest-status query from another workspace."""
    with workspace_scope(11):
        store.create(
            scheduled_task_id=_uid("lst_w11"),
            name="w11",
            prompt="p",
            rrule="FREQ=MINUTELY",
            user_id="a",
            agent_id=_uid("ag"),
            timezone="UTC",
        )
        store.create_run(
            run_id=_uid("lst_w11_run"),
            scheduled_task_id=_uid("lst_w11"),
            status="succeeded",
            scheduled_at=100,
            finished_at=105,
        )
    assert store.list_latest_run_status_for_tasks([_uid("lst_w11")]) == {}
    with workspace_scope(11):
        assert store.list_latest_run_status_for_tasks([_uid("lst_w11")]) == {
            _uid("lst_w11"): "succeeded"
        }


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


def _due_task(store: SqlAlchemyScheduledTaskStore, seed: str = "dispatch"):
    store.configure_dispatch(True)
    task = store.create(
        _uid(seed),
        seed,
        "prompt",
        "FREQ=HOURLY",
        None,
        _uid("agent"),
        "UTC",
        host_id=_uid("host"),
    )
    # Discovery and claiming consume DB snapshots (Uuid16 normalizes UUIDs on read).
    return store.get(task.id)


def test_dispatch_claim_is_atomic_across_replicas(store: SqlAlchemyScheduledTaskStore) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    task = _due_task(store)
    assert task is not None and task.next_run_at is not None
    barrier = Barrier(2)

    def claim():
        replica = SqlAlchemyScheduledTaskStore(store.storage_location)
        barrier.wait(timeout=10)
        return replica.claim_scheduled_run(
            task,
            now=task.next_run_at,
            next_run_at=task.next_run_at + 3600,
            host_id=task.host_id,
            lease_seconds=120,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    runs = [run for run in results if run is not None]
    assert len(runs) == 1
    run = runs[0]
    assert run.status == "starting"
    assert run.trigger_key == f"schedule:{task.next_run_at}"
    assert run.host_id == task.host_id
    assert run.lease_expires_at == task.next_run_at + 120
    assert store.get(task.id).next_run_at == task.next_run_at + 3600
    assert store.list_runs(task.id)[0] == [run]
    assert store.list_latest_run_status_for_tasks([task.id]) == {task.id: "starting"}


@pytest.mark.parametrize(
    "edit", [{"prompt": "new"}, {"host_id": _uid("new-host")}, {"state": "paused"}]
)
def test_dispatch_rejects_edited_snapshot(store: SqlAlchemyScheduledTaskStore, edit: dict) -> None:
    task = _due_task(store)
    updated = store.update(task.id, **edit)
    assert (
        store.claim_scheduled_run(
            task,
            now=task.next_run_at,
            next_run_at=task.next_run_at + 3600,
            host_id=task.host_id,
            lease_seconds=120,
        )
        is None
    )
    assert store.get(task.id) == updated
    assert store.list_runs(task.id)[0] == []


@pytest.mark.parametrize("duplicate_occurrence", [False, True])
def test_dispatch_insert_failure_rolls_back_schedule(
    store: SqlAlchemyScheduledTaskStore, duplicate_occurrence: bool
) -> None:
    from sqlalchemy import event
    from sqlalchemy.exc import IntegrityError, StatementError

    from omnigent.db.db_models import SqlScheduledTaskRun

    def insert_duplicate(mapper, connection, run):
        connection.execute(
            SqlScheduledTaskRun.__table__.insert().values(
                workspace_id=run.workspace_id,
                id=_uid("conflicting_run"),
                scheduled_task_id=run.scheduled_task_id,
                scheduled_at=run.scheduled_at,
                status=run.status,
                trigger_key=run.trigger_key,
            )
        )

    task = _due_task(store)
    if duplicate_occurrence:
        event.listen(SqlScheduledTaskRun, "before_insert", insert_duplicate)
    try:
        with pytest.raises(IntegrityError if duplicate_occurrence else StatementError):
            store.claim_scheduled_run(
                task,
                now=task.next_run_at,
                next_run_at=task.next_run_at + 3600,
                host_id=task.host_id if duplicate_occurrence else "not-a-uuid",
                lease_seconds=120,
            )
    finally:
        if duplicate_occurrence:
            event.remove(SqlScheduledTaskRun, "before_insert", insert_duplicate)
    assert store.get(task.id) == task
    assert store.list_runs(task.id)[0] == []


def test_dispatch_pagination_with_equal_times(store: SqlAlchemyScheduledTaskStore) -> None:
    from sqlalchemy import update

    from omnigent.db.db_models import SqlScheduledTask

    tasks = []
    for workspace in (31, 32, 33):
        with workspace_scope(workspace):
            for seed in ("first", "second"):
                tasks.append(_due_task(store, seed))
    cutoff = max(task.next_run_at for task in tasks)
    with store._session_immediate("test_equal_due_times") as session:
        session.execute(update(SqlScheduledTask).values(next_run_at=cutoff))
    seen = []
    cursor = None
    while page := store.list_due_tasks(cutoff, after=cursor, limit=1):
        task = page[0]
        seen.append((task.workspace_id, task.id))
        cursor = (task.next_run_at, task.workspace_id, task.id)
    assert seen == sorted((task.workspace_id, task.id) for task in tasks)


def test_dispatch_mutations_require_workspace_scope(store: SqlAlchemyScheduledTaskStore) -> None:
    with workspace_scope(31):
        task = _due_task(store)
    now = task.next_run_at
    assert (
        store.claim_scheduled_run(
            task, now=now, next_run_at=now + 3600, host_id=task.host_id, lease_seconds=120
        )
        is None
    )
    with workspace_scope(task.workspace_id):
        run = store.claim_scheduled_run(
            task, now=now, next_run_at=now + 3600, host_id=task.host_id, lease_seconds=120
        )
    assert store.renew_starting_run(run.id, now=lambda: now + 30, lease_seconds=120) is None
    assert store.transition_starting_run(run, now=lambda: now + 30, status="failed") is None


def test_startup_lease_renewal_expiry_and_conditional_reconciliation(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    task = _due_task(store)
    now = task.next_run_at
    run = store.claim_scheduled_run(
        task, now=now, next_run_at=now + 3600, host_id=task.host_id, lease_seconds=120
    )
    renewed = store.renew_starting_run(run.id, now=lambda: now + 30, lease_seconds=120)
    assert renewed.lease_expires_at == now + 150
    assert store.transition_starting_run(run, now=lambda: now + 31, status="failed") is None
    assert store.list_expired_starting_runs(now + 120) == []
    assert store.list_expired_starting_runs(now + 150) == [renewed]
    assert store.renew_starting_run(run.id, now=lambda: now + 150, lease_seconds=120) == renewed
    assert store.transition_starting_run(renewed, now=lambda: now + 150, status="running") is None
    # A reconciler that established prompt delivery preserves the running session.
    running = store.transition_starting_run(
        renewed,
        now=lambda: now + 150,
        status="running",
        expired=True,
        conversation_id=_uid("session"),
    )
    assert running.status == "running"
    assert running.lease_expires_at is None
    assert store.list_expired_starting_runs(now + 300) == []
    assert store.renew_starting_run(run.id, now=lambda: now + 300, lease_seconds=120) == running
    assert (
        store.transition_starting_run(
            renewed, now=lambda: now + 300, status="failed", expired=True
        )
        is None
    )


def test_schedule_edits_and_run_now_metadata(store: SqlAlchemyScheduledTaskStore) -> None:
    task = _due_task(store)
    assert task.next_run_at > task.created_at
    metadata_update = store.update(task.id, last_run_at=task.created_at + 1)
    assert metadata_update.next_run_at == task.next_run_at
    assert store.update(task.id, state="paused").next_run_at is None
    assert store.update(task.id, state="active").next_run_at is not None
    assert store.update(task.id, state="deleted").next_run_at is None


@pytest.mark.parametrize("dispatch_enabled", [False, True])
def test_metadata_update_does_not_calculate_null_cursor(store, monkeypatch, dispatch_enabled):
    from omnigent.stores.scheduled_task_store import sqlalchemy_store

    task = _due_task(store)
    # A paused task has no next occurrence; neither metadata nor prompt edits
    # should invoke recurrence search, even if the rule would exhaust its budget.
    store.update(task.id, state="paused")
    store.configure_dispatch(dispatch_enabled)

    def unexpected_calculation(*args):
        pytest.fail("Metadata-only writes must not calculate a schedule")

    monkeypatch.setattr(sqlalchemy_store, "next_scheduled_time", unexpected_calculation)
    updated = store.update(task.id, last_run_at=task.created_at + 1, prompt="edited")
    assert updated.next_run_at is None
    assert updated.last_run_at == task.created_at + 1
    assert updated.prompt == "edited"


def test_duplicate_occurrence_advances_cursor_without_another_run(store):
    from sqlalchemy import update

    from omnigent.db.db_models import SqlScheduledTask

    task = _due_task(store)
    now = task.next_run_at
    run = store.claim_scheduled_run(
        task, now=now, next_run_at=now + 3600, host_id=task.host_id, lease_seconds=120
    )
    # A clock correction followed by a schedule edit can revisit an occurrence.
    with store._session_immediate("test_revisited_occurrence") as session:
        session.execute(
            update(SqlScheduledTask).where(SqlScheduledTask.id == task.id).values(next_run_at=now)
        )
    assert (
        store.claim_scheduled_run(
            store.get(task.id),
            now=now,
            next_run_at=now + 3600,
            host_id=task.host_id,
            lease_seconds=120,
        )
        is None
    )
    assert store.list_due_tasks(now) == []
    assert store.get(task.id).next_run_at == now + 3600
    assert store.list_runs(task.id)[0] == [run]


def test_invalid_schedule_pause_does_not_overwrite_edits_or_other_workspace(store):
    task = _due_task(store)
    with workspace_scope(42):
        assert not store.pause_invalid_schedule(task)
    store.update(task.id, rrule="FREQ=DAILY")
    assert not store.pause_invalid_schedule(task)
    assert store.get(task.id).state == "active"
    assert store.get(task.id).rrule == "FREQ=DAILY"


def test_legacy_schedule_initialization_does_not_overwrite_edit(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    from sqlalchemy import update

    from omnigent.db.db_models import SqlScheduledTask

    task = _due_task(store)
    with store._session_immediate("test_legacy_task") as session:
        session.execute(
            update(SqlScheduledTask).where(SqlScheduledTask.id == task.id).values(next_run_at=None)
        )
    legacy = store.get(task.id)
    store.update(task.id, prompt="edited")
    assert not store.initialize_next_run_at(legacy, task.next_run_at)
    assert store.get(task.id).next_run_at is None
    assert store.initialize_next_run_at(store.get(task.id), task.next_run_at)
    assert not store.initialize_next_run_at(store.get(task.id), task.next_run_at)


@pytest.mark.parametrize("finish_running_first", [False, True])
def test_deleted_task_keeps_claims_until_last_completion(
    store: SqlAlchemyScheduledTaskStore, finish_running_first: bool
) -> None:
    task = _due_task(store, "delete_claims")
    now = task.next_run_at
    first = store.claim_scheduled_run(
        task, now=now, next_run_at=now + 3600, host_id=task.host_id, lease_seconds=120
    )
    running = store.transition_starting_run(first, now=lambda: now, status="running")
    second = store.claim_scheduled_run(
        store.get(task.id),
        now=now + 3600,
        next_run_at=now + 7200,
        host_id=task.host_id,
        lease_seconds=120,
    )
    assert store.delete(task.id)
    assert not store.delete(task.id)
    assert store.get(task.id).state == "deleted"
    assert store.get(task.id).next_run_at is None
    assert store.update(task.id, state="active") is None
    assert store.list_due_tasks(now + 7200) == []
    assert len(store.list_runs(task.id)[0]) == 2

    def complete():
        result = store.update_run(running.id, status="succeeded", finished_at=now + 3720)
        assert result.status == "succeeded"

    def recover():
        result = store.transition_starting_run(
            second, now=lambda: now + 3720, status="failed", expired=True
        )
        assert result.status == "failed"

    first_finish, last_finish = (
        (complete, recover) if finish_running_first else (recover, complete)
    )
    first_finish()
    assert store.get(task.id).state == "deleted"
    assert len(store.list_runs(task.id)[0]) == 1
    last_finish()
    assert store.get(task.id) is None
    assert store.list_runs(task.id)[0] == []


def test_delete_racing_claim_preserves_any_claim_that_won(
    store: SqlAlchemyScheduledTaskStore,
) -> None:
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    task = _due_task(store, "delete_race")
    barrier = Barrier(2)

    def claim():
        barrier.wait(timeout=10)
        return store.claim_scheduled_run(
            task,
            now=task.next_run_at,
            next_run_at=task.next_run_at + 3600,
            host_id=task.host_id,
            lease_seconds=120,
        )

    def delete_task():
        barrier.wait(timeout=10)
        return store.delete(task.id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claiming = pool.submit(claim)
        deleting = pool.submit(delete_task)
        run = claiming.result(timeout=10)
        assert deleting.result(timeout=10)
    if run is None:
        assert store.get(task.id) is None
        assert store.list_runs(task.id)[0] == []
    else:
        assert store.get(task.id).state == "deleted"
        assert store.list_expired_starting_runs(task.next_run_at + 120) == [run]


def test_cursor_reset_commits_bounded_pages_and_can_resume(store, monkeypatch):
    from sqlalchemy import update

    from omnigent.db.db_models import SqlScheduledTask
    from omnigent.db.enum_codecs import encode_scheduled_task_state
    from omnigent.stores.scheduled_task_store import sqlalchemy_store

    monkeypatch.setattr(store, "_cleanup_batch_size", 2)
    tasks = []
    for workspace in (31, 32):
        with workspace_scope(workspace):
            for index in range(4):
                tasks.append(_due_task(store, f"reset-{index}"))
    with store._session_immediate("test_paused_cursor") as session:
        session.execute(
            update(SqlScheduledTask)
            .where(SqlScheduledTask.workspace_id == 32)
            .values(state=encode_scheduled_task_state("paused"))
        )
    original = sqlalchemy_store.run_write_transaction
    original_cursor_after = store._cursor_after
    cursor_bounds = []
    pages = 0

    def record_cursor(columns, cursor):
        cursor_bounds.append(cursor)
        return original_cursor_after(columns, cursor)

    monkeypatch.setattr(store, "_cursor_after", record_cursor)

    def fail_second_page(maker, operation, callback):
        nonlocal pages
        if operation == "reset_dispatch_cursor_page":
            pages += 1
            if pages == 2:
                raise RuntimeError("reset interrupted")
        return original(maker, operation, callback)

    monkeypatch.setattr(sqlalchemy_store, "run_write_transaction", fail_second_page)
    with pytest.raises(RuntimeError, match="reset interrupted"):
        store.reset_dispatch_cursors()
    remaining = []
    for task in tasks:
        with workspace_scope(task.workspace_id):
            remaining.append(store.get(task.id).next_run_at)
    assert remaining.count(None) == 2
    store.reset_dispatch_cursors()
    task_keys = sorted((task.next_run_at, task.workspace_id, task.id) for task in tasks)
    # Resume after committed pages, using the original timestamps even after
    # they are cleared. Each state starts its own scan from the beginning.
    assert cursor_bounds == [task_keys[3], task_keys[5], task_keys[7]]
    cursor_bounds.clear()
    store.reset_dispatch_cursors()
    assert cursor_bounds == []
    for task in tasks:
        with workspace_scope(task.workspace_id):
            assert store.get(task.id).next_run_at is None


def test_uninitialized_discovery_pages_across_workspaces(store):
    tasks = []
    for workspace in (31, 32):
        with workspace_scope(workspace):
            for seed in ("a", "b"):
                task = _due_task(store, seed)
                tasks.append(task)
            store.update(task.id, state="paused")
    store.reset_dispatch_cursors()
    cursor = None
    seen = []
    while page := store.list_uninitialized_tasks(after=cursor, limit=1):
        task = page[0]
        seen.append((task.workspace_id, task.id))
        cursor = (task.workspace_id, task.id)
        with workspace_scope(task.workspace_id):
            assert store.initialize_next_run_at(task, task.created_at + 3600)
    assert seen == sorted((task.workspace_id, task.id) for task in tasks[::2])
    assert store.list_uninitialized_tasks() == []


def test_interrupted_history_cleanup_resumes_without_losing_live_claim(store, monkeypatch):
    monkeypatch.setattr(store, "_cleanup_batch_size", 2)
    with workspace_scope(31):
        task = _due_task(store, "cleanup")
        run = store.claim_scheduled_run(
            task,
            now=task.next_run_at,
            next_run_at=task.next_run_at + 3600,
            host_id=None,
            lease_seconds=120,
        )
        for index in range(7):
            store.create_run(_uid(f"old-{index}"), task.id, "succeeded", index)
        original = store._purge_deleted_task_page
        pages = 0

        def fail_second_page(task_id, *, after=None):
            nonlocal pages
            pages += 1
            if pages == 2:
                raise RuntimeError("cleanup interrupted")
            return original(task_id, after=after)

        monkeypatch.setattr(store, "_purge_deleted_task_page", fail_second_page)
        assert store.delete(task.id)
        assert store.get(task.id).state == "deleted"
        assert len(store.list_runs(task.id)[0]) == 6
    # The background retry crosses workspace boundaries, but preserves the live run.
    cursor = None
    for _ in range(10):
        cursor = store.purge_deleted_tasks(after=cursor, limit=1)
        if cursor is None:
            break
    else:
        pytest.fail("Cleanup did not complete its scan")
    with workspace_scope(31):
        assert store.list_runs(task.id)[0] == [run]
        store.transition_starting_run(run, now=lambda: task.next_run_at, status="failed")
        assert store.get(task.id) is None


def test_dispatch_support_rejects_d1(store, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(
        store, "_engine", SimpleNamespace(dialect=SimpleNamespace(name="cloudflare_d1"))
    )
    with pytest.raises(NotImplementedError, match="multi-statement transactions"):
        store.check_dispatch_support()


@pytest.mark.parametrize("fired_at_present", [False, True])
def test_deleted_running_claim_expires_without_a_read(store, monkeypatch, fired_at_present):
    from sqlalchemy import update

    from omnigent.db.db_models import SqlScheduledTaskRun
    from omnigent.server.scheduled.run_reconciler import STALE_RUN_MAX_AGE_SECONDS
    from omnigent.stores.scheduled_task_store import sqlalchemy_store

    task = _due_task(store)
    scheduled = task.next_run_at
    fired = scheduled + 60
    run = store.claim_scheduled_run(
        task, now=fired, next_run_at=fired + 3600, host_id=None, lease_seconds=120
    )
    store.transition_starting_run(run, now=lambda: fired, status="running")
    if not fired_at_present:
        with store._session_immediate("test_missing_dispatch_time") as session:
            session.execute(
                update(SqlScheduledTaskRun)
                .where(SqlScheduledTaskRun.id == run.id)
                .values(fired_at=None)
            )
    age_from = fired if fired_at_present else scheduled
    monkeypatch.setattr(
        sqlalchemy_store, "now_epoch", lambda: age_from + STALE_RUN_MAX_AGE_SECONDS - 1
    )
    assert store.delete(task.id)
    assert store.list_runs(task.id)[0][0].status == "running"
    monkeypatch.setattr(
        sqlalchemy_store, "now_epoch", lambda: age_from + STALE_RUN_MAX_AGE_SECONDS
    )
    store.purge_deleted_tasks()
    failed = store.list_runs(task.id)[0][0]
    assert failed.status == "failed"
    assert failed.error_code == "incomplete"
    assert failed.finished_at == age_from + STALE_RUN_MAX_AGE_SECONDS
    assert store.update_run(run.id, status="succeeded", finished_at=failed.finished_at) is None
    cursor = None
    for _ in range(3):
        cursor = store.purge_deleted_tasks(after=cursor)
    assert store.get(task.id) is None
    assert store.list_runs(task.id)[0] == []


def test_deleted_cleanup_pages_past_live_claims_across_tasks(store, monkeypatch):
    monkeypatch.setattr(store, "_cleanup_batch_size", 2)
    retained = []
    for workspace in (31, 32):
        with workspace_scope(workspace):
            task = _due_task(store, "retained")
            for index in range(3):
                task = store.get(task.id)
                store.claim_scheduled_run(
                    task,
                    now=task.next_run_at,
                    next_run_at=task.next_run_at + 3600,
                    host_id=None,
                    lease_seconds=120,
                )
            for index in range(3):
                store.create_run(
                    _uid(f"finished-{index}"),
                    task.id,
                    "succeeded",
                    task.next_run_at + 7200 + index,
                )
            # Simulate an interruption just after committing the deletion marker.
            store.update(task.id, state="deleted")
            retained.append((workspace, task.id))
    cursor = None
    for _ in range(20):
        cursor = store.purge_deleted_tasks(after=cursor, limit=1)
        if cursor is None:
            break
    else:
        pytest.fail("Cleanup did not progress past retained claims")
    for workspace, task_id in retained:
        with workspace_scope(workspace):
            assert store.get(task_id).state == "deleted"
            runs, _ = store.list_runs(task_id)
            assert len(runs) == 3
            assert all(run.status == "starting" for run in runs)
