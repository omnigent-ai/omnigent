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


def test_host_recovery_has_a_fixed_work_bound_and_skips_ordinary_hosts(store, monkeypatch):
    from unittest.mock import Mock
    from uuid import UUID, uuid4

    from sqlalchemy import event, insert

    from omnigent.db.db_models import SqlConversationMetadata
    from omnigent.server.sandbox_profiles import SandboxProfileCatalog
    from omnigent.stores.host_store import HostStore

    host_id = uuid4().hex
    hosts = HostStore(store.storage_location)
    hosts.upsert_on_connect(host_id, "ordinary", "alice")
    with store._session_immediate("seed_large_host_history") as session:
        session.execute(
            insert(SqlConversationMetadata),
            [
                {"id": uuid4().hex, "host_id": host_id, "workspace": "/workspace"}
                for _ in range(250)
            ],
        )
    catalog = SandboxProfileCatalog(store, None, hosts)
    lookup = Mock(wraps=store.list_host_conversation_ids)
    monkeypatch.setattr(store, "list_host_conversation_ids", lookup)
    assert catalog.get_host_snapshot(host_id) is None
    lookup.assert_not_called()

    reference = store.create_conversation(host_id=host_id, workspace="/workspace")
    profiles = SandboxProfileStore(store)
    profiles.bind_session(reference.id, PROFILE)
    for supplied_host in (host_id, f"host_{host_id}", str(UUID(host_id))):
        assert (
            profiles.get_host_snapshot(supplied_host, reference_session_id=reference.id) == PROFILE
        )
    lookup.assert_not_called()

    hydrate = Mock(side_effect=AssertionError("Overflow must not hydrate session history"))
    monkeypatch.setattr(store, "get_conversation", hydrate)
    queries = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if statement.lstrip().upper().startswith("SELECT"):
            queries.append((statement, parameters))

    event.listen(store._engine, "before_cursor_execute", capture)
    try:
        with pytest.raises(ValueError, match="lookup limit"):
            SandboxProfileStore(store).get_host_snapshot(host_id)
    finally:
        event.remove(store._engine, "before_cursor_execute", capture)
    assert len(queries) == 1
    statement, parameters = queries[0]
    with store._engine.connect() as connection:
        assert len(connection.exec_driver_sql(statement, parameters).all()) == 201
        if store._engine.dialect.name == "sqlite":
            plan = str(
                connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters).all()
            )
            assert "ix_conversation_metadata_host_id" in plan
            assert "SCAN" not in plan and "TEMP B-TREE" not in plan
    hydrate.assert_not_called()


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


def test_profile_binding_queries_stay_bounded_with_many_unrelated_labels(store):
    from uuid import uuid4

    from sqlalchemy import event

    labels = {f"customer:{index}": "value" for index in range(1000)}
    conversation = store.create_conversation(labels=labels)
    profiles = SandboxProfileStore(store)
    host_id = uuid4().hex
    queries = []

    def capture(_conn, _cursor, statement, parameters, _context, _many):
        if "conversation_labels" in statement and statement.lstrip().upper().startswith(
            ("SELECT", "DELETE")
        ):
            queries.append((statement, parameters))

    event.listen(store._conv_engine, "before_cursor_execute", capture)
    try:
        before = store.get_profile_host_binding(conversation.id)
        assert profiles.get_session_snapshot(conversation.id) is None
        assert profiles.get_session_host_id(conversation.id) is None
        previous = store.bind_profiled_host(
            conversation.id, host_id, "/workspace", PROFILE, runner_id="attempt"
        )
        assert previous == before
        assert store.get_profile_host_binding(conversation.id).profile_snapshot is not None
        assert profiles.get_session_snapshot(conversation.id) == PROFILE
        assert profiles.get_session_host_id(conversation.id) == host_id
        assert profiles.get_host_snapshot(host_id) == PROFILE
        assert profiles.get_host_snapshot(host_id, reference_session_id=conversation.id) == PROFILE
        assert profiles.get_host_snapshot(host_id, reference_session_id=uuid4().hex) == PROFILE
        with pytest.raises(ValueError, match="cannot be changed"):
            store.accept_sandbox_profile(conversation.id, {**PROFILE, "revision": "v2"})
        assert store.rollback_profiled_host(
            previous, expected_host_id=host_id, expected_runner_id="attempt"
        )
    finally:
        event.remove(store._conv_engine, "before_cursor_execute", capture)

    assert queries
    with store._conv_engine.connect() as connection:
        for statement, parameters in queries:
            if statement.lstrip().upper().startswith("SELECT"):
                assert len(connection.exec_driver_sql(statement, parameters).all()) <= 7
            if store._conv_engine.dialect.name == "sqlite":
                plan = str(
                    connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters).all()
                )
                assert "key=?" in plan and "SCAN" not in plan
    current = store.get_conversation(conversation.id)
    assert current.host_id is None and current.runner_id is None
    expected = labels if store._conv_engine is store._engine else labels | profile_labels(PROFILE)
    assert current.labels == expected


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


@pytest.mark.parametrize("legacy_key", ["sandbox_profile:team", "sandbox_profile:id"])
def test_legacy_partial_labels_do_not_block_ordinary_host_changes(store, legacy_key):
    from uuid import uuid4

    from omnigent.db.db_models import SqlConversationLabel
    from omnigent.stores.host_store import HostStore

    hosts = HostStore(store.storage_location)
    first, second = uuid4().hex, uuid4().hex
    for host_id in (first, second):
        hosts.upsert_on_connect(host_id, "ordinary-host", "alice")
    conversation = store.create_conversation(host_id=first, workspace="/workspace")
    with store._conv_session_immediate("seed_legacy_partial_profile") as transaction:
        transaction.add(
            SqlConversationLabel(
                conversation_id=conversation.id,
                key=legacy_key,
                value="build",
                updated_at=0,
            )
        )
    store.set_host_id(conversation.id, second, "/new-workspace")
    assert store.get_conversation(conversation.id).host_id == second
    store.clear_host_binding(conversation.id)
    assert store.get_conversation(conversation.id).host_id is None
    if legacy_key == "sandbox_profile:id":
        with pytest.raises(ValueError, match="Incomplete saved sandbox profile"):
            SandboxProfileStore(store).get_session_snapshot(conversation.id)
    else:
        assert SandboxProfileStore(store).get_session_snapshot(conversation.id) is None


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
