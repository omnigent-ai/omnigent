"""Shared-preference transactions against the suite's real database backend."""

from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from omnigent.db.db_models import SqlPreference, workspace_scope
from omnigent.errors import OmnigentError
from omnigent.preference_sync import PreferenceMutation, PreferenceSnapshot
from omnigent.stores.preference_store.sqlalchemy_store import SqlAlchemyPreferenceStore


@pytest.fixture
def store(db_uri):
    return SqlAlchemyPreferenceStore(db_uri, authority_id="test-authority")


def mutation(store, *, user="alice", key=None, value="model-a", version=0, resolves=()):
    return PreferenceMutation(
        protocol_version=1,
        identity=store.identity(user),
        operation_id=uuid4(),
        installation_id="laptop",
        key=key or {"kind": "model", "name": "model"},
        expected_version=version,
        value=value,
        resolves=list(resolves),
    )


@pytest.mark.parametrize(
    "key,value",
    [
        ({"kind": "model", "name": "model"}, "model-a"),
        ({"kind": "model", "name": "opencode_model"}, "model-b"),
        ({"kind": "harness", "name": "default"}, "pi-native"),
        ({"kind": "harness_args", "name": "claude"}, []),
        (
            {
                "kind": "plugin",
                "name": {"ecosystem": "claude", "source": "catalog", "name": "tools"},
            },
            False,
        ),
    ],
)
def test_roundtrip_and_receipt_survive_new_store(store, db_uri, key, value):
    operation = mutation(store, key=key, value=value)
    first = store.apply(operation, user_id="alice")
    reopened = SqlAlchemyPreferenceStore(db_uri, authority_id="test-authority")
    assert reopened.apply(operation, user_id="alice") == first
    snapshot = PreferenceSnapshot.model_validate_json(
        reopened.read(user_id="alice").model_dump_json()
    )
    assert snapshot.revision == 1
    assert snapshot.entries == [first.entry]
    assert snapshot.as_layer().values[operation.key.as_key()] == (
        tuple(value) if isinstance(value, list) else value
    )


def test_independent_keys_and_delayed_receipt(store):
    original = mutation(store)
    receipt = store.apply(original, user_id="alice")
    harness = mutation(store, key={"kind": "harness", "name": "default"}, value="pi-native")
    store.apply(harness, user_id="alice")
    newer = mutation(store, value="model-b", version=1)
    store.apply(newer, user_id="alice")
    assert store.apply(original, user_id="alice") == receipt
    snapshot = store.read(user_id="alice")
    assert snapshot.revision == 3
    assert {entry.key.kind: entry.value for entry in snapshot.entries} == {
        "model": "model-b",
        "harness": "pi-native",
    }
    changed_retry = original.model_copy(update={"value": "model-c"})
    with pytest.raises(OmnigentError, match="Operation ID"):
        store.apply(changed_retry, user_id="alice")


def test_tombstone_and_stale_identical_edit(store):
    store.apply(mutation(store), user_id="alice")
    removal = mutation(store, value=None, version=1)
    deleted = store.apply(removal, user_id="alice")
    assert deleted.entry.version == 2
    assert store.read(user_id="alice").as_layer().values == {}
    conflict = store.apply(mutation(store, value="model-c", version=1), user_id="alice")
    assert conflict.status == "conflict"
    assert conflict.entry.value is None
    converged = store.apply(mutation(store, value=None, version=0), user_id="alice")
    assert converged.status == "unchanged"
    assert converged.entry.version == 2
    assert store.apply(removal, user_id="alice") == deleted


def test_first_remove_retains_version_against_offline_creation(store):
    assert store.apply(mutation(store, value=None), user_id="alice").entry.version == 1
    assert store.apply(mutation(store), user_id="alice").status == "conflict"


def test_conflict_alternatives_resolution_and_stale_resolution(store):
    store.apply(mutation(store), user_id="alice")
    alternatives = [mutation(store, value=value) for value in ("model-b", "model-c")]
    for operation in alternatives:
        assert store.apply(operation, user_id="alice").status == "conflict"
    assert len(store.read(user_id="alice").conflicts) == 2
    store.apply(mutation(store, value="model-d", version=1), user_id="alice")
    stale = mutation(store, value="model-b", version=1, resolves=[alternatives[0].operation_id])
    assert store.apply(stale, user_id="alice").status == "conflict"
    snapshot = store.read(user_id="alice")
    assert len(snapshot.conflicts) == 3
    resolved = mutation(
        store, value="model-b", version=2, resolves=[op.operation_id for op in snapshot.conflicts]
    )
    result = store.apply(resolved, user_id="alice")
    assert result.entry.version == 3
    assert store.read(user_id="alice").conflicts == []
    assert store.apply(resolved, user_id="alice") == result
    assert store.apply(alternatives[0], user_id="alice").status == "conflict"
    assert store.read(user_id="alice").conflicts == []


def test_reject_invalid_resolution_without_changes(store):
    with pytest.raises(OmnigentError, match="Conflict no longer"):
        store.apply(mutation(store, resolves=[uuid4()]), user_id="alice")
    with pytest.raises(OmnigentError, match="ahead"):
        store.apply(mutation(store, version=1), user_id="alice")
    assert store.read(user_id="alice").revision == 0


def test_isolation_and_read_does_not_initialize(store, db_uri):
    for user in (None, "alice", "bob"):
        assert store.read(user_id=user).entries == []
    with store._engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(SqlPreference)) == 0
    operation = mutation(store)
    store.apply(operation, user_id="alice")
    assert store.read(user_id=None).entries == []
    assert store.read(user_id="bob").entries == []
    with pytest.raises(OmnigentError, match="authenticated context"):
        store.apply(operation, user_id="bob")
    with workspace_scope(17):
        assert store.read(user_id="alice").entries == []
        with pytest.raises(OmnigentError, match="authenticated context"):
            store.apply(operation, user_id="alice")
        store.apply(mutation(store, value="workspace-model"), user_id="alice")
    other = SqlAlchemyPreferenceStore(db_uri, authority_id="other-authority")
    with pytest.raises(OmnigentError, match="authority"):
        other.read(user_id="alice")
    with pytest.raises(OmnigentError, match="authority"):
        other.apply(mutation(other), user_id="alice")
    assert store.read(user_id="alice").entries[0].value == "model-a"


def test_concurrent_stores_preserve_competing_edits(store, db_uri):
    second = SqlAlchemyPreferenceStore(db_uri, authority_id="test-authority")
    operations = [mutation(store, value="model-a"), mutation(second, value="model-b")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda pair: pair[0].apply(pair[1], user_id="alice"),
                zip([store, second], operations, strict=True),
            )
        )
    assert sorted(result.status for result in results) == ["applied", "conflict"]
    snapshot = store.read(user_id="alice")
    assert snapshot.revision == 2
    assert len(snapshot.conflicts) == 1
    assert {snapshot.entries[0].value, snapshot.conflicts[0].value} == {"model-a", "model-b"}


def test_history_limit_preserves_receipts(store, monkeypatch):
    import omnigent.stores.preference_store.sqlalchemy_store as module

    monkeypatch.setattr(module, "MAX_OPERATIONS", 1)
    operation = mutation(store)
    result = store.apply(operation, user_id="alice")
    with pytest.raises(OmnigentError, match="history is full"):
        store.apply(mutation(store, version=1, value="model-b"), user_id="alice")
    assert store.apply(operation, user_id="alice") == result
    assert store.read(user_id="alice").revision == 1


def test_key_and_conflict_limits_preserve_existing_values(store, monkeypatch):
    import omnigent.stores.preference_store.sqlalchemy_store as module

    monkeypatch.setattr(module, "MAX_KEYS", 1)
    monkeypatch.setattr(module, "MAX_CONFLICTS", 1)
    store.apply(mutation(store), user_id="alice")
    with pytest.raises(OmnigentError, match="key limit"):
        store.apply(
            mutation(store, key={"kind": "model", "name": "opencode_model"}), user_id="alice"
        )
    alternative = mutation(store, value="model-b")
    store.apply(alternative, user_id="alice")
    with pytest.raises(OmnigentError, match="outstanding"):
        store.apply(mutation(store, value="model-c"), user_id="alice")
    resolved = mutation(store, version=1, resolves=[alternative.operation_id])
    assert store.apply(resolved, user_id="alice").status == "unchanged"
    assert store.read(user_id="alice").conflicts == []
    assert store.apply(mutation(store, value="model-d"), user_id="alice").status == "conflict"


def test_account_deletion_cleans_preferences_and_revokes_stale_write(store, db_uri):
    from omnigent.db.account_authority import account_authority_scope
    from omnigent.server.accounts_store import SqlAlchemyAccountStore

    accounts = SqlAlchemyAccountStore(db_uri)
    accounts.create_user_with_password("alice", "test-hash", is_admin=False)
    operation = mutation(store)
    store.apply(operation, user_id="alice")
    generation = accounts.login_snapshot("alice")[1]
    assert generation is not None
    assert accounts.delete_user("alice") is True
    assert store.read(user_id="alice").entries == []
    with account_authority_scope("alice", generation):
        with pytest.raises(OmnigentError, match="revoked"):
            store.apply(operation, user_id="alice")
    with store._engine.connect() as connection:
        assert connection.scalar(select(func.count()).select_from(SqlPreference)) == 0
