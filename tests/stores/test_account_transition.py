"""A pending account move persists with the detach, on both database layouts."""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import event

from omnigent.errors import OmnigentError
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture(params=["conversation_store", "split_db_conversation_store"])
def store(request) -> SqlAlchemyConversationStore:
    return request.getfixturevalue(request.param)


def destination() -> dict:
    return {
        "id": "transition-one",
        "account_id": "sample-account",
        "host_id": "b" * 32,
        "harness": "codex-native",
        "model": "sample-model",
        "effort": "high",
    }


def test_detach_and_marker_commit_atomically_and_survive_store_restart(store) -> None:
    conv = store.create_conversation(
        runner_id="runner-before", host_id="a" * 32, workspace="/tmp/project", title="Keep history"
    )
    store.set_labels(conv.id, {"user-label": "kept"})
    committed = []

    def observe(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE"):
            committed.append(statement)

    event.listen(store._engine, "before_cursor_execute", observe)
    try:
        result = store.begin_account_transition(
            conv.id, destination(), expected_runner_id="runner-before"
        )
    finally:
        event.remove(store._engine, "before_cursor_execute", observe)
    assert len(committed) == 1
    assert "runner_id=" in committed[0] and "account_transition=" in committed[0]
    restarted = SqlAlchemyConversationStore(
        store.storage_location, store.conversation_storage_location
    )
    fresh = restarted.get_conversation(conv.id)
    assert fresh.runner_id is None
    assert fresh.account_transition["id"] == "transition-one"
    assert fresh.account_transition["previous_runner_id"] == "runner-before"
    assert fresh.account_transition["previous_host_id"] == "a" * 32
    assert fresh.workspace == "/tmp/project"
    assert fresh.title == "Keep history"
    assert fresh.labels == {"user-label": "kept"}
    assert result.account_transition == fresh.account_transition


def test_abandoned_retry_is_idempotent_and_cannot_retarget(store) -> None:
    conv = store.create_conversation(runner_id="prior")
    first = store.begin_account_transition(conv.id, destination(), expected_runner_id="prior")
    second = store.begin_account_transition(
        conv.id, {**destination(), "id": "retry-id"}, expected_runner_id=None
    )
    assert second.account_transition == first.account_transition
    with pytest.raises(OmnigentError, match="pending"):
        store.begin_account_transition(
            conv.id, {**destination(), "host_id": "c" * 32}, expected_runner_id=None
        )


@pytest.mark.parametrize("busy", ["running", "waiting"])
def test_busy_or_changed_runner_never_detaches(store, busy) -> None:
    conv = store.create_conversation(runner_id="prior")
    store.set_session_live_status(conv.id, busy)
    with pytest.raises(OmnigentError):
        store.begin_account_transition(conv.id, destination(), expected_runner_id="prior")
    fresh = store.get_conversation(conv.id)
    assert fresh.runner_id == "prior"
    assert fresh.account_transition is None


def test_completion_requires_matching_launch_and_generic_writes_cannot_clear_marker(store) -> None:
    conv = store.create_conversation(runner_id="prior")
    store.begin_account_transition(conv.id, destination(), expected_runner_id="prior")
    store.set_labels(conv.id, {"account_transition": "", "omnigent.account": "fake"})
    store.update_conversation(conv.id, model_override="different")
    store.clear_host_binding(conv.id)
    assert store.get_conversation(conv.id).account_transition["id"] == "transition-one"
    assert store.bind_account_launch(
        conv.id, "transition-one", "new-runner", "b" * 32, "/tmp/destination"
    )
    for transition_id, runner in [("stale", "new-runner"), ("transition-one", "prior")]:
        with pytest.raises(OmnigentError):
            store.finish_account_transition(conv.id, transition_id, runner)
    store.finish_account_transition(conv.id, "transition-one", "new-runner")
    assert store.get_conversation(conv.id).account_transition is None


def test_delayed_recovery_and_launch_cleanup_cannot_clear_replacement(store) -> None:
    conv = store.create_conversation(runner_id="prior", host_id="a" * 32, workspace="/tmp/project")
    store.begin_account_transition(conv.id, destination(), expected_runner_id="prior")
    assert store.bind_account_launch(
        conv.id, "transition-one", "old-attempt", "b" * 32, "/tmp/destination"
    )
    store.reset_account_launch(conv.id, "transition-one", "old-attempt")
    assert store.bind_account_launch(
        conv.id, "transition-one", "new-attempt", "b" * 32, "/tmp/destination"
    )
    with pytest.raises(OmnigentError, match="binding changed"):
        store.reset_account_launch(conv.id, "transition-one", "old-attempt")
    with pytest.raises(OmnigentError, match="binding changed"):
        store.clear_host_binding(conv.id, expected_runner_id="old-attempt")
    fresh = store.get_conversation(conv.id)
    assert fresh.runner_id == "new-attempt"
    assert fresh.host_id == "b" * 32
    assert fresh.account_transition["launch_runner_id"] == "new-attempt"


def test_runner_binds_other_than_the_recorded_launch_are_refused(store) -> None:
    conv = store.create_conversation(runner_id="prior", host_id="a" * 32, workspace="/tmp/project")
    store.begin_account_transition(conv.id, destination(), expected_runner_id="prior")
    assert store.set_runner_id(conv.id, "stray") is False
    for runner in ["stray", "prior"]:
        with pytest.raises(OmnigentError, match="pending"):
            store.replace_runner_id(conv.id, runner)
        with pytest.raises(OmnigentError, match="pending"):
            store.replace_runner_id(conv.id, runner, expected_runner_id=None)
    fresh = store.get_conversation(conv.id)
    assert fresh.runner_id is None
    assert store.bind_account_launch(
        conv.id, "transition-one", "dest", "b" * 32, "/tmp/destination"
    )


def test_stray_bind_and_two_concurrent_launches_leave_exactly_one_runner(store) -> None:
    conv = store.create_conversation(runner_id="prior", host_id="a" * 32, workspace="/tmp/project")
    store.begin_account_transition(conv.id, destination(), expected_runner_id="prior")
    barrier = threading.Barrier(3)

    def stray() -> bool:
        barrier.wait()
        return store.set_runner_id(conv.id, "stray")

    def launch(runner: str) -> bool:
        barrier.wait()
        return store.bind_account_launch(
            conv.id, "transition-one", runner, "b" * 32, "/tmp/destination"
        )

    with ThreadPoolExecutor(3) as pool:
        results = [
            pool.submit(stray),
            pool.submit(launch, "dest-1"),
            pool.submit(launch, "dest-2"),
        ]
    stray_won, first, second = (result.result() for result in results)
    assert stray_won is False
    assert [first, second].count(True) == 1
    fresh = store.get_conversation(conv.id)
    assert fresh.runner_id in {"dest-1", "dest-2"}
    assert fresh.account_transition["launch_runner_id"] == fresh.runner_id
    store.finish_account_transition(conv.id, "transition-one", fresh.runner_id)


def test_abandon_clears_the_marker_and_leaves_the_session_detached(store) -> None:
    conv = store.create_conversation(runner_id="prior", host_id="a" * 32, workspace="/tmp/project")
    store.begin_account_transition(conv.id, destination(), expected_runner_id="prior")
    assert store.bind_account_launch(
        conv.id, "transition-one", "attempt", "b" * 32, "/tmp/destination"
    )
    with pytest.raises(OmnigentError, match="replaced"):
        store.abandon_account_transition(conv.id, "transition-one", expected_launch_runner_id=None)
    with pytest.raises(OmnigentError, match="replaced"):
        store.abandon_account_transition(conv.id, "stale", expected_launch_runner_id="attempt")
    result = store.abandon_account_transition(
        conv.id, "transition-one", expected_launch_runner_id="attempt"
    )
    assert result.account_transition is None
    assert result.runner_id is None
    # A new move can begin at once, to any destination.
    again = store.begin_account_transition(
        conv.id, {**destination(), "id": "second", "host_id": "c" * 32}, expected_runner_id=None
    )
    assert again.account_transition["host_id"] == "c" * 32


def test_abandon_before_any_launch_and_when_nothing_is_pending(store) -> None:
    conv = store.create_conversation(runner_id="prior")
    store.begin_account_transition(conv.id, destination(), expected_runner_id="prior")
    store.abandon_account_transition(conv.id, "transition-one", expected_launch_runner_id=None)
    with pytest.raises(OmnigentError, match="replaced"):
        store.abandon_account_transition(conv.id, "transition-one", expected_launch_runner_id=None)


def test_marker_over_the_byte_cap_is_refused_without_detaching(store) -> None:
    conv = store.create_conversation(runner_id="prior")
    with pytest.raises(OmnigentError, match="too large"):
        store.begin_account_transition(
            conv.id, {**destination(), "workspace": "/" + "w" * 9000}, expected_runner_id="prior"
        )
    fresh = store.get_conversation(conv.id)
    assert fresh.runner_id == "prior"
    assert fresh.account_transition is None
