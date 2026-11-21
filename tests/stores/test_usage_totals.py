"""Stored spawn-tree and project usage totals.

Each usage write adds its change to the tree's top-level ``subtree_usage`` and
that session's project's ``project_usage`` in the same transaction, so both
always equal what summing the member sessions would give.
"""

from __future__ import annotations

import json
import math
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from omnigent.db.compression import encode
from omnigent.db.db_models import (
    USAGE_TOTALS_MAX_STORED_BYTES,
    SqlConversationMetadata,
    current_workspace_id,
)
from omnigent.db.utils import now_epoch
from omnigent.runtime.policies.builder import load_session_usage
from omnigent.stores.conversation_store import add_usage_totals, usage_totals_change
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.project_store.sqlalchemy_store import SqlAlchemyProjectStore

_OWNER = "owner@example.com"


@pytest.fixture()
def projects(db_uri: str) -> SqlAlchemyProjectStore:
    return SqlAlchemyProjectStore(db_uri)


def _project(projects: SqlAlchemyProjectStore, name: str) -> str:
    return projects.create(uuid.uuid4().hex, name, _OWNER).id


def _tree_totals(store: SqlAlchemyConversationStore, session_id: str) -> dict[str, Any] | None:
    with Session(store._engine) as session:
        raw = session.scalar(
            select(SqlConversationMetadata.subtree_usage).where(
                SqlConversationMetadata.workspace_id == current_workspace_id(),
                SqlConversationMetadata.id == session_id,
            )
        )
    return None if raw is None else json.loads(raw)


def _displayed(store: SqlAlchemyConversationStore, session_id: str) -> dict[str, Any]:
    """The subtree usage the session view shows, without the enforcement-only cost."""
    usage = load_session_usage(session_id, store)
    usage.pop("policy_cost_usd", None)
    return usage


def _turn(tokens: int, cost: float | None, model: str = "model-a") -> dict[str, Any]:
    counts = {
        "input_tokens": tokens,
        "output_tokens": tokens // 2,
        "total_tokens": tokens * 3 // 2,
    }
    priced = {} if cost is None else {"total_cost_usd": cost}
    return {**counts, **priced, "by_model": {model: {**counts, **priced}}}


def _assert_usage_equal(actual: dict[str, Any] | None, expected: dict[str, Any]) -> None:
    """Compare a stored total with a session-view sum, which has no priced-session count."""
    assert actual is not None
    actual = {key: value for key, value in actual.items() if key != "priced_sessions"}
    assert actual.keys() == expected.keys()
    for key, value in expected.items():
        if key == "by_model":
            assert actual[key].keys() == value.keys()
            for model, bucket in value.items():
                _assert_usage_equal(actual[key][model], bucket)
        else:
            assert actual[key] == pytest.approx(value)


def test_change_covers_summed_counters_only() -> None:
    old = {
        "input_tokens": 100,
        "total_cost_usd": 1.0,
        "policy_cost_usd": 5.0,
        "subagent_costs_usd": {"child": 2.0},
        "native_cost_complete": True,
        "by_model": {"model-a": {"input_tokens": 100, "total_cost_usd": 1.0}},
    }
    new = {
        "input_tokens": 150,
        "output_tokens": True,
        "cache_read_input_tokens": math.nan,
        "total_cost_usd": 1.0,
        "policy_cost_usd": 9.0,
        "subagent_costs_usd": {"child": 4.0},
        "by_model": {
            "model-a": {"input_tokens": 120, "total_cost_usd": 1.0},
            "model-b": {"input_tokens": 30, "total_cost_usd": 0.0},
        },
    }

    change = usage_totals_change(old, new)

    assert change == {
        "input_tokens": 50,
        "by_model": {
            "model-a": {"input_tokens": 20},
            "model-b": {"input_tokens": 30, "total_cost_usd": 0.0, "priced_sessions": 1},
        },
    }
    assert usage_totals_change(new, new) == {}


def test_added_change_reaches_the_new_usage_and_never_goes_negative() -> None:
    old = _turn(100, 0.25)
    new = _turn(40, None, model="model-b")
    totals = json.loads(json.dumps(old))

    add_usage_totals(totals, usage_totals_change(old, new))

    assert totals["input_tokens"] == 40
    # The only priced session became unpriced, so the total no longer has a cost.
    assert "total_cost_usd" not in totals
    assert totals["by_model"]["model-a"] == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    assert totals["by_model"]["model-b"]["input_tokens"] == 40

    rounding = {"total_cost_usd": 0.1 + 0.2, "priced_sessions": 1}
    add_usage_totals(rounding, {"total_cost_usd": -0.30000000000000004 - 1e-12})
    assert rounding == {"total_cost_usd": 0, "priced_sessions": 1}


def test_a_dropped_breakdown_spreads_to_totals_it_moves_into() -> None:
    dropped = {"input_tokens": 5, "by_model_dropped": True}
    project = {"input_tokens": 7, "by_model": {"model-a": {"input_tokens": 7}}}

    add_usage_totals(project, usage_totals_change({}, dropped))
    add_usage_totals(project, usage_totals_change({}, _turn(1, None)))

    assert project == {
        "input_tokens": 13,
        "output_tokens": 0,
        "total_tokens": 1,
        "by_model_dropped": True,
    }


def test_relay_and_native_writes_keep_tree_and_project_totals_current(
    conversation_store: SqlAlchemyConversationStore, projects: SqlAlchemyProjectStore
) -> None:
    project_id = _project(projects, "Factory")
    root = conversation_store.create_conversation(project_id=project_id)
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    grandchild = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=child.id
    )
    assert _tree_totals(conversation_store, root.id) == {}
    assert _tree_totals(conversation_store, child.id) is None

    conversation_store.increment_session_usage(root.id, _turn(1000, 0.5))
    conversation_store.increment_session_usage(child.id, _turn(400, 0.2, model="model-b"))
    conversation_store.set_session_usage(
        grandchild.id,
        {**_turn(300, 0.3), "policy_cost_usd": 0.9, "subagent_costs_usd": {"x": 0.4}},
    )
    # A native report may lower SET counters; the totals follow it down.
    conversation_store.set_session_usage(grandchild.id, _turn(200, 0.3))

    expected = _displayed(conversation_store, root.id)
    assert expected["input_tokens"] == 1600
    assert expected["total_cost_usd"] == pytest.approx(1.0)
    _assert_usage_equal(_tree_totals(conversation_store, root.id), expected)
    _assert_usage_equal(projects.get_usage(project_id, user_id=_OWNER), expected)
    assert projects.get_usage(project_id, user_id="someone-else@example.com") is None


def test_unpriced_usage_stays_unpriced_and_untracked_sessions_count_nothing(
    conversation_store: SqlAlchemyConversationStore, projects: SqlAlchemyProjectStore
) -> None:
    project_id = _project(projects, "Factory")
    unpriced = conversation_store.create_conversation(project_id=project_id)
    older = conversation_store.create_conversation(project_id=project_id)
    older_child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=older.id
    )
    with Session(conversation_store._engine) as session, session.begin():
        session.execute(
            update(SqlConversationMetadata)
            .where(SqlConversationMetadata.id == older.id)
            .values(subtree_usage=None)
        )

    conversation_store.increment_session_usage(unpriced.id, _turn(500, None))
    conversation_store.increment_session_usage(older.id, _turn(100, 1.0))
    conversation_store.set_session_usage(older_child.id, _turn(100, 1.0))

    assert _tree_totals(conversation_store, older.id) is None
    totals = projects.get_usage(project_id, user_id=_OWNER)
    assert totals is not None
    assert totals["input_tokens"] == 500
    assert "total_cost_usd" not in totals


@pytest.mark.asyncio
async def test_deleting_sessions_takes_their_usage_out_of_the_totals(
    conversation_store: SqlAlchemyConversationStore, projects: SqlAlchemyProjectStore
) -> None:
    project_id = _project(projects, "Factory")
    root = conversation_store.create_conversation(project_id=project_id)
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    grandchild = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=child.id
    )
    other = conversation_store.create_conversation(project_id=project_id)
    conversation_store.increment_session_usage(root.id, _turn(1000, 1.0))
    conversation_store.increment_session_usage(child.id, _turn(200, 2.0))
    conversation_store.increment_session_usage(grandchild.id, _turn(30, 3.0))
    conversation_store.increment_session_usage(other.id, _turn(4, 4.0))

    assert await conversation_store.delete_conversation(child.id)

    _assert_usage_equal(_tree_totals(conversation_store, root.id), _turn(1000, 1.0))
    assert projects.get_usage(project_id, user_id=_OWNER)["total_cost_usd"] == pytest.approx(5.0)

    assert await conversation_store.delete_conversation(root.id)

    _assert_usage_equal(projects.get_usage(project_id, user_id=_OWNER), _turn(4, 4.0))


@pytest.mark.asyncio
async def test_a_kept_host_reservation_stops_counting_toward_its_project(
    db_uri: str, conversation_store: SqlAlchemyConversationStore, projects: SqlAlchemyProjectStore
) -> None:
    host_id = uuid.uuid4().hex
    HostStore(db_uri).register_managed_host(
        host_id=host_id,
        name="sandbox",
        user_id=_OWNER,
        token="sandbox-token",
        provider="modal",
        sandbox_id="sandbox",
        token_expires_at=now_epoch() + 1000,
    )
    project_id = _project(projects, "Factory")
    session = conversation_store.create_conversation(
        project_id=project_id, host_id=host_id, workspace="/work"
    )
    conversation_store.increment_session_usage(session.id, _turn(10, 1.0))

    assert await conversation_store.delete_conversation(session.id, preserve_host_binding=host_id)

    assert _tree_totals(conversation_store, session.id) is None
    assert "total_cost_usd" not in projects.get_usage(project_id, user_id=_OWNER)
    replacement = conversation_store.create_conversation(
        conversation_id=session.id, host_id=host_id, workspace="/work", project_id=project_id
    )
    assert _tree_totals(conversation_store, replacement.id) == {}


def test_refiling_moves_totals_between_projects(
    conversation_store: SqlAlchemyConversationStore, projects: SqlAlchemyProjectStore
) -> None:
    first = _project(projects, "First")
    second = _project(projects, "Second")
    session = conversation_store.create_conversation(project_id=first)
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=session.id
    )
    conversation_store.increment_session_usage(session.id, _turn(100, 1.0))
    conversation_store.increment_session_usage(child.id, _turn(10, 0.5))
    representative = conversation_store.create_conversation(project_id=first)
    peer = conversation_store.create_conversation()
    conversation_store.add_new_sibling(representative.id, peer.id, user_id=_OWNER)
    conversation_store.increment_session_usage(representative.id, _turn(1, 2.0))
    conversation_store.increment_session_usage(peer.id, _turn(2, 4.0))

    assert conversation_store.set_conversation_project(session.id, second)
    assert conversation_store.set_conversation_project(representative.id, second)

    assert "total_cost_usd" not in projects.get_usage(first, user_id=_OWNER)
    assert projects.get_usage(second, user_id=_OWNER)["input_tokens"] == 113
    assert projects.get_usage(second, user_id=_OWNER)["total_cost_usd"] == pytest.approx(7.5)

    assert conversation_store.set_conversation_project(session.id, None)

    assert projects.get_usage(second, user_id=_OWNER)["total_cost_usd"] == pytest.approx(6.0)


def test_nesting_a_session_moves_its_total_to_the_family_project(
    conversation_store: SqlAlchemyConversationStore, projects: SqlAlchemyProjectStore
) -> None:
    family_project = _project(projects, "Family")
    other_project = _project(projects, "Other")
    source = conversation_store.create_conversation(project_id=family_project)
    moved = conversation_store.create_conversation(project_id=other_project)
    conversation_store.increment_session_usage(moved.id, _turn(50, 1.5))

    conversation_store.set_session_nest(moved.id, source.id, user_id=_OWNER)

    assert conversation_store.get_conversation(moved.id).project_id == family_project
    assert "total_cost_usd" not in projects.get_usage(other_project, user_id=_OWNER)
    assert projects.get_usage(family_project, user_id=_OWNER)["total_cost_usd"] == pytest.approx(
        1.5
    )


def test_forks_start_their_own_tree_total(
    conversation_store: SqlAlchemyConversationStore, projects: SqlAlchemyProjectStore
) -> None:
    project_id = _project(projects, "Factory")
    source = conversation_store.create_conversation(project_id=project_id)
    conversation_store.increment_session_usage(source.id, _turn(100, 1.0))

    fork = conversation_store.fork_conversation(source.id, project_id=project_id)
    conversation_store.increment_session_usage(fork.id, _turn(10, 0.25))

    assert _tree_totals(conversation_store, fork.id)["total_cost_usd"] == pytest.approx(0.25)
    assert projects.get_usage(project_id, user_id=_OWNER)["total_cost_usd"] == pytest.approx(1.25)


def test_concurrent_writers_in_one_tree_lose_no_usage(
    conversation_store: SqlAlchemyConversationStore, projects: SqlAlchemyProjectStore
) -> None:
    project_id = _project(projects, "Factory")
    root = conversation_store.create_conversation(project_id=project_id)
    children = [
        conversation_store.create_conversation(kind="sub_agent", parent_conversation_id=root.id).id
        for _ in range(3)
    ]

    def write(index: int) -> None:
        if index % 2:
            conversation_store.increment_session_usage(root.id, _turn(10, 0.01))
        else:
            # Cumulative native reports race to overwrite each child's usage.
            child = children[index % len(children)]
            conversation_store.set_session_usage(
                child, {"cache_read_input_tokens": index, "total_cost_usd": index / 100}
            )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(80)))

    expected = _displayed(conversation_store, root.id)
    assert expected["input_tokens"] == 400
    _assert_usage_equal(_tree_totals(conversation_store, root.id), expected)
    _assert_usage_equal(projects.get_usage(project_id, user_id=_OWNER), expected)


@pytest.mark.asyncio
async def test_totals_span_split_databases(
    split_db_conversation_store: SqlAlchemyConversationStore,
) -> None:
    store = split_db_conversation_store
    # Projects live with the session metadata, not the conversation rows.
    projects = SqlAlchemyProjectStore(store.storage_location)
    project_id = _project(projects, "Factory")
    root = store.create_conversation(project_id=project_id)
    child = store.create_conversation(kind="sub_agent", parent_conversation_id=root.id)
    store.increment_session_usage(root.id, _turn(100, 1.0))
    store.set_session_usage(child.id, _turn(50, 0.5))

    _assert_usage_equal(projects.get_usage(project_id, user_id=_OWNER), _displayed(store, root.id))

    assert await store.delete_conversation(child.id)

    _assert_usage_equal(projects.get_usage(project_id, user_id=_OWNER), _turn(100, 1.0))


def test_an_oversized_per_model_breakdown_is_dropped_but_counters_stay_exact(
    conversation_store: SqlAlchemyConversationStore, projects: SqlAlchemyProjectStore
) -> None:
    project_id = _project(projects, "Factory")
    root = conversation_store.create_conversation(project_id=project_id)
    models = {
        f"model-{uuid.uuid4().hex}": {"input_tokens": index, "total_cost_usd": index / 7}
        for index in range(400)
    }
    assert len(encode(json.dumps({"by_model": models}))) > USAGE_TOTALS_MAX_STORED_BYTES

    conversation_store.increment_session_usage(
        root.id, {"input_tokens": 79_800, "total_cost_usd": 1.0, "by_model": models}
    )
    # A later small change must not rebuild a breakdown that misses the earlier usage.
    conversation_store.increment_session_usage(root.id, _turn(10, 0.5))

    expected = {
        "input_tokens": 79_810,
        "output_tokens": 5,
        "total_tokens": 15,
        "total_cost_usd": 1.5,
        "priced_sessions": 1,
        "by_model_dropped": True,
    }
    assert _tree_totals(conversation_store, root.id) == expected
    assert projects.get_usage(project_id, user_id=_OWNER) == expected


@pytest.mark.asyncio
async def test_removing_the_last_priced_session_leaves_the_project_unpriced(
    conversation_store: SqlAlchemyConversationStore, projects: SqlAlchemyProjectStore
) -> None:
    project_id = _project(projects, "Factory")
    priced = conversation_store.create_conversation(project_id=project_id)
    unpriced = conversation_store.create_conversation(project_id=project_id)
    conversation_store.increment_session_usage(priced.id, _turn(10, 1.0))
    conversation_store.increment_session_usage(unpriced.id, _turn(20, None))
    assert projects.get_usage(project_id, user_id=_OWNER)["total_cost_usd"] == 1.0

    assert conversation_store.set_conversation_project(priced.id, None)

    totals = projects.get_usage(project_id, user_id=_OWNER)
    assert totals["input_tokens"] == 20
    assert "total_cost_usd" not in totals
    assert "total_cost_usd" not in totals["by_model"]["model-a"]

    # A genuinely priced $0.00 still shows, until that session leaves too.
    free = conversation_store.create_conversation(project_id=project_id)
    conversation_store.increment_session_usage(free.id, _turn(5, 0.0))
    assert projects.get_usage(project_id, user_id=_OWNER)["total_cost_usd"] == 0
    assert await conversation_store.delete_conversation(free.id)
    assert "total_cost_usd" not in projects.get_usage(project_id, user_id=_OWNER)


@pytest.mark.asyncio
async def test_usage_landing_mid_deletion_never_skews_the_totals(
    conversation_store: SqlAlchemyConversationStore,
    projects: SqlAlchemyProjectStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A report between a deletion's conversation and metadata deletes counts nowhere."""
    import omnigent.stores.conversation_store.sqlalchemy_store as store_module

    project_id = _project(projects, "Factory")
    root = conversation_store.create_conversation(project_id=project_id)
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    conversation_store.increment_session_usage(root.id, _turn(100, 1.0))
    conversation_store.increment_session_usage(child.id, _turn(10, 0.5))
    real_write = store_module.run_write_transaction
    raced: list[str] = []

    def racing_write(session_maker: Any, operation_name: str, callback: Any, **kwargs: Any) -> Any:
        if operation_name == "delete_conversation_metadata" and not raced:
            raced.append(operation_name)
            conversation_store.increment_session_usage(child.id, _turn(1000, 9.0))
            conversation_store.set_session_usage(child.id, _turn(2000, 9.0))
        return real_write(session_maker, operation_name, callback, **kwargs)

    monkeypatch.setattr(store_module, "run_write_transaction", racing_write)

    assert await conversation_store.delete_conversation(child.id)

    assert raced
    _assert_usage_equal(_tree_totals(conversation_store, root.id), _turn(100, 1.0))
    _assert_usage_equal(projects.get_usage(project_id, user_id=_OWNER), _turn(100, 1.0))


def test_whole_tree_deletion_waits_for_a_child_usage_write(
    conversation_store: SqlAlchemyConversationStore, projects: SqlAlchemyProjectStore
) -> None:
    """A child's usage write and its tree's deletion lock rows in one order, so neither fails.

    The writer pauses holding the child's row until the deletion's metadata
    cleanup has started; on engines with row locks the cleanup then waits for it.
    """
    import asyncio
    import threading

    project_id = _project(projects, "Factory")
    root = conversation_store.create_conversation(project_id=project_id)
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=root.id
    )
    conversation_store.increment_session_usage(root.id, _turn(100, 1.0))
    conversation_store.increment_session_usage(child.id, _turn(10, 0.5))
    writer_holds_child = threading.Event()
    cleanup_started = threading.Event()
    errors: list[BaseException] = []
    lock_tree = conversation_store._lock_tree_usage_totals
    remove_deleted = conversation_store._remove_deleted_usage

    def paused_lock_tree(session: Any, root_id: Any) -> Any:
        if threading.current_thread().name == "usage-writer":
            writer_holds_child.set()
            # SQLite serializes the two transactions, so the cleanup never starts here.
            if cleanup_started.wait(timeout=2):
                time.sleep(0.5)
        return lock_tree(session, root_id)

    def signalling_remove_deleted(*args: Any, **kwargs: Any) -> Any:
        cleanup_started.set()
        return remove_deleted(*args, **kwargs)

    def run(target: Any) -> None:
        try:
            target()
        except BaseException as error:  # Surfaced by the assertion below.
            errors.append(error)

    conversation_store._lock_tree_usage_totals = paused_lock_tree  # type: ignore[method-assign]
    conversation_store._remove_deleted_usage = signalling_remove_deleted  # type: ignore[method-assign]
    writer = threading.Thread(
        name="usage-writer",
        target=run,
        args=(lambda: conversation_store.increment_session_usage(child.id, _turn(1, 2.0)),),
    )
    writer.start()
    assert writer_holds_child.wait(timeout=10)
    deleter = threading.Thread(
        target=run, args=(lambda: asyncio.run(conversation_store.delete_conversation(root.id)),)
    )
    deleter.start()
    writer.join(timeout=60)
    deleter.join(timeout=60)

    assert not writer.is_alive() and not deleter.is_alive()
    assert errors == []
    totals = projects.get_usage(project_id, user_id=_OWNER)
    assert "total_cost_usd" not in totals
    assert totals.get("input_tokens", 0) == 0
