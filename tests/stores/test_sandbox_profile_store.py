"""Profile references use protected labels in both single- and split-DB deployments."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import inspect

from omnigent.db.db_models import workspace_scope
from omnigent.stores.sandbox_profile_store import SandboxProfileStore, profile_labels

PROFILE = {
    "provider": "company",
    "id": "build",
    "revision": "v1",
    "name": "Build",
    "description": "x" * 500,
    "user_id": "alice",
    "workspace_id": 0,
}


@pytest.fixture(params=["conversation_store", "split_db_conversation_store"])
def store(request):
    return request.getfixturevalue(request.param)


def test_reference_survives_restart_without_schema_changes(store):
    session = store.create_conversation()
    profiles = SandboxProfileStore(store)
    profiles.bind_session(session.id, PROFILE)
    restarted = type(store)(store.storage_location, store.conversation_storage_location)
    assert SandboxProfileStore(restarted).get_session_snapshot(session.id) == PROFILE
    assert "sandbox_profile_bindings" not in inspect(store._engine).get_table_names()
    assert all(
        "runner_host" not in index["name"]
        for index in inspect(store._engine).get_indexes("omnigent_conversation_metadata")
    )
    assert all(len(value) <= 256 for value in store.get_conversation(session.id).labels.values())
    with workspace_scope(42):
        assert profiles.get_session_snapshot(session.id) is None


@pytest.mark.parametrize("operation", ["create", "set", "delete", "fork_extra"])
def test_generic_label_writers_cannot_forge_or_clear_a_profile(store, operation):
    session = store.create_conversation()
    SandboxProfileStore(store).bind_session(session.id, PROFILE)
    labels = {"sandbox_profile:id": "forged"}
    with pytest.raises(ValueError, match="server-owned"):
        if operation == "create":
            store.create_conversation(labels=labels)
        elif operation == "set":
            store.set_labels(session.id, labels)
        elif operation == "delete":
            store.delete_label(session.id, "sandbox_profile:id")
        else:
            store.fork_conversation(session.id, extra_labels=labels)
    assert SandboxProfileStore(store).get_session_snapshot(session.id) == PROFILE


@pytest.mark.asyncio
async def test_fork_and_deletion_do_not_preserve_untrusted_copies(store):
    source = store.create_conversation(labels={"team": "build"})
    profiles = SandboxProfileStore(store)
    profiles.bind_session(source.id, PROFILE)
    fork = store.fork_conversation(source.id)
    assert fork.labels["team"] == "build"
    assert profiles.get_session_snapshot(fork.id) is None
    profiles.bind_session(fork.id, PROFILE)
    await store.delete_conversation(source.id)
    assert profiles.get_session_snapshot(source.id) is None
    assert profiles.get_session_snapshot(fork.id) == PROFILE


def test_competing_acceptance_is_immutable(store):
    session = store.create_conversation()
    profiles = SandboxProfileStore(store)

    def accept(revision):
        try:
            profiles.bind_session(session.id, {**PROFILE, "revision": revision})
            return revision
        except ValueError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(accept, ["first", "second"]))
    assert sum(result is not None for result in results) == 1
    assert profiles.get_session_snapshot(session.id)["revision"] in results


def test_oversized_identity_is_rejected_without_partial_labels(store):
    session = store.create_conversation()
    profiles = SandboxProfileStore(store)
    with pytest.raises(ValueError, match="label value limit"):
        profiles.bind_session(session.id, {**PROFILE, "user_id": "a" * 257})
    assert store.get_conversation(session.id).labels == {}
    assert profile_labels(PROFILE)["sandbox_profile:description:1"] == "x" * 250


def test_failed_metadata_commit_never_leaves_a_runner_or_mutable_profile(store, monkeypatch):
    from contextlib import contextmanager
    from uuid import uuid4

    session = store.create_conversation()
    host_id = uuid4().hex
    original = store._session_immediate

    @contextmanager
    def failing_commit(operation):
        with original(operation) as transaction:
            yield transaction
            if operation == "bind_profiled_host":
                raise RuntimeError("metadata commit failed")

    failing_commit.query_name_prefix = original.query_name_prefix
    failing_commit.engine = original.engine
    monkeypatch.setattr(store, "_session_immediate", failing_commit)
    with pytest.raises(RuntimeError, match="metadata commit failed"):
        store.bind_profiled_host(session.id, host_id, "/workspace", PROFILE, runner_id="runner")
    after = store.get_conversation(session.id)
    assert after.host_id is None and after.runner_id is None
    profiles = SandboxProfileStore(store)
    split = store._engine is not store._conv_engine
    assert profiles.get_session_snapshot(session.id) == (PROFILE if split else None)
    if split:
        with pytest.raises(ValueError, match="cannot be changed"):
            profiles.bind_session(session.id, {**PROFILE, "id": "another"})
    monkeypatch.setattr(store, "_session_immediate", original)
    store.bind_profiled_host(session.id, host_id, "/workspace", PROFILE, runner_id="retry")
    assert profiles.get_session_snapshot(session.id) == PROFILE
    assert store.get_conversation(session.id).runner_id == "retry"
