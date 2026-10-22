"""Shared-preference transactions against the suite's real database backend."""

import json
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from sqlalchemy import LargeBinary, event, func, select, type_coerce

from omnigent.db.compression import decode
from omnigent.db.db_models import SqlPreference, workspace_scope
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.preference_sync import PreferenceMutation, PreferenceSnapshot
from omnigent.stores.preference_store.sqlalchemy_store import (
    SqlAlchemyPreferenceStore,
    _fingerprint,
)


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


def test_receipt_fingerprint_is_stable():
    operation = PreferenceMutation(
        protocol_version=1,
        identity={"authority_id": "a", "user_id": "u", "workspace_id": "0"},
        operation_id=UUID(int=1),
        installation_id="laptop",
        key={"kind": "harness_args", "name": "claude"},
        expected_version=2,
        value=["--profile", "工具"],
        resolves=[UUID(int=2)],
    )
    assert _fingerprint(operation) == (
        "00fc8acaa5a97271a412cef2f4a68acccc9ebef1c2f2f304dc479889495fec12"
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
        reopened.read(user_id="alice").model_dump_json(exclude={"next_cursor"})
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
    for changes in (
        {"value": "model-c"},
        {"installation_id": "desktop"},
        {"expected_version": 1},
        {"resolves": [uuid4()]},
    ):
        with pytest.raises(OmnigentError, match="Operation ID"):
            store.apply(original.model_copy(update=changes), user_id="alice")
    assert store.read(user_id="alice") == snapshot
    assert store.apply(original, user_id="alice") == receipt


def test_maximum_value_has_compact_durable_receipt(store, db_uri):
    operation = mutation(store, value="é" * 4090)
    result = store.apply(operation, user_id="alice")
    reopened = SqlAlchemyPreferenceStore(db_uri, authority_id="test-authority")
    assert reopened.apply(operation, user_id="alice") == result
    with store._session("inspect_record_sizes") as session:
        rows = session.execute(
            select(SqlPreference.key, type_coerce(SqlPreference.value, LargeBinary))
        ).all()
    assert all(len(row.value) < 16384 for row in rows)
    assert all(len((decode(row.value) or "").encode()) < 16384 for row in rows)
    receipt = next(row for row in rows if row.key.startswith("shared.v1.receipt."))
    body = json.loads(decode(receipt.value))
    assert "operation" not in body
    assert len(body["fingerprint"]) == 64


def test_oversized_conflict_preserves_state_and_operation_id(db_uri):
    # Escaped names and many resolution IDs can exceed the full-record cap.
    name = "\x01" * 128
    store = SqlAlchemyPreferenceStore(db_uri, authority_id=name)
    key = {"kind": "harness_args", "name": name}
    store.apply(mutation(store, user=name, key=key, value=[]), user_id=name)
    conflicts = [mutation(store, user=name, key=key, value=["alternative"]) for _ in range(127)]
    for conflict in conflicts:
        assert store.apply(conflict, user_id=name).status == "conflict"
    oversized = mutation(
        store,
        user=name,
        key=key,
        value=["x" * 8178],
        resolves=[conflict.operation_id for conflict in conflicts],
    ).model_copy(update={"installation_id": name})
    assert len(oversized.model_dump_json(include={"value"}).encode()) == 8192
    assert len(oversized.model_dump_json().encode()) > 16000
    with store._session("inspect_before_rejected_edit") as session:
        before = session.execute(select(SqlPreference.key, SqlPreference.value)).all()
    with pytest.raises(OmnigentError, match="storage limit") as error:
        store.apply(oversized, user_id=name)
    assert error.value.code == ErrorCode.INVALID_INPUT
    with store._session("inspect_after_rejected_edit") as session:
        assert dict(session.execute(select(SqlPreference.key, SqlPreference.value)).all()) == dict(
            before
        )
    corrected = oversized.model_copy(update={"value": ["small"], "resolves": []})
    accepted = store.apply(corrected, user_id=name)
    assert accepted.status == "conflict"
    assert store.apply(corrected, user_id=name) == accepted


@pytest.mark.parametrize("initial_write", [False, True])
def test_tombstone_and_stale_identical_edit(store, initial_write):
    if initial_write:
        store.apply(mutation(store), user_id="alice")
    removal = mutation(store, value=None, version=int(initial_write))
    deleted = store.apply(removal, user_id="alice")
    assert deleted.entry.version == 1 + initial_write
    assert all(entry.value is None for entry in store.read(user_id="alice").entries)
    conflict = store.apply(
        mutation(store, value="model-c", version=int(initial_write)), user_id="alice"
    )
    assert conflict.status == "conflict"
    assert conflict.entry.value is None
    converged = store.apply(mutation(store, value=None, version=0), user_id="alice")
    assert converged.status == "unchanged"
    assert converged.entry.version == deleted.entry.version
    assert store.apply(removal, user_id="alice") == deleted


def test_conflict_alternatives_resolution_and_stale_resolution(store):
    store.apply(mutation(store), user_id="alice")
    alternatives = [mutation(store, value=value) for value in ("model-b", "model-c")]
    for operation in alternatives:
        assert store.apply(operation, user_id="alice").status == "conflict"
    partial = mutation(store, value="model-b", version=1, resolves=[alternatives[0].operation_id])
    assert store.apply(partial, user_id="alice").status == "applied"
    assert store.read(user_id="alice").conflicts == [alternatives[1]]
    # Add a new alternative before the stale/full resolution checks.
    assert store.apply(mutation(store, value="model-c"), user_id="alice").status == "conflict"
    store.apply(mutation(store, value="model-d", version=2), user_id="alice")
    stale = mutation(store, value="model-b", version=1, resolves=[alternatives[1].operation_id])
    assert store.apply(stale, user_id="alice").status == "conflict"
    snapshot = store.read(user_id="alice")
    assert len(snapshot.conflicts) == 3
    resolved = mutation(
        store, value="model-b", version=3, resolves=[op.operation_id for op in snapshot.conflicts]
    )
    result = store.apply(resolved, user_id="alice")
    assert result.entry.version == 4
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
    store.apply(mutation(store), user_id="alice")
    conflict = mutation(store, value="other")
    store.apply(conflict, user_id="alice")
    before = store.read(user_id="alice")
    wrong_key = mutation(
        store,
        key={"kind": "harness", "name": "default"},
        value="pi-native",
        resolves=[conflict.operation_id],
    )
    with pytest.raises(OmnigentError, match="Conflict no longer"):
        store.apply(wrong_key, user_id="alice")
    assert store.read(user_id="alice") == before
    # Rejection must not reserve the operation ID.
    assert (
        store.apply(wrong_key.model_copy(update={"resolves": []}), user_id="alice").status
        == "applied"
    )


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


@pytest.mark.parametrize("same_operation", [False, True])
def test_concurrent_stores_preserve_competing_edits(store, db_uri, same_operation):
    second = SqlAlchemyPreferenceStore(db_uri, authority_id="test-authority")
    operations = [mutation(store, value=f"model-{index}") for index in range(20)]
    if same_operation:
        operations = [operations[0]] * len(operations)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(
                lambda pair: pair[0].apply(pair[1], user_id="alice"),
                zip([store, second] * 10, operations, strict=True),
            )
        )
    snapshot = store.read(user_id="alice")
    if same_operation:
        assert all(result == results[0] for result in results)
        assert snapshot.revision == 1 and snapshot.conflicts == []
    else:
        assert sorted(result.revision for result in results) == list(range(1, 21))
        assert [result.status for result in results].count("applied") == 1
        assert snapshot.revision == 20 and len(snapshot.conflicts) == 19
    assert {snapshot.entries[0].value, *(conflict.value for conflict in snapshot.conflicts)} == {
        operation.value for operation in operations
    }


def test_history_limit_preserves_receipts(store, monkeypatch):
    import omnigent.stores.preference_store.sqlalchemy_store as module

    monkeypatch.setattr(module, "MAX_OPERATIONS", 2)
    operation = mutation(store)
    result = store.apply(operation, user_id="alice")
    conflict = mutation(store, value="alternative")
    store.apply(conflict, user_id="alice")
    before = store.read(user_id="alice")
    with pytest.raises(OmnigentError, match="history is full"):
        store.apply(mutation(store, version=1, value="model-b"), user_id="alice")
    with pytest.raises(OmnigentError, match="history is full"):
        store.apply(
            mutation(store, version=1, value="alternative", resolves=[conflict.operation_id]),
            user_id="alice",
        )
    assert store.apply(operation, user_id="alice") == result
    assert store.read(user_id="alice") == before
    assert before.conflicts == [conflict]


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


@pytest.mark.parametrize("limit", [1, 3, 100])
def test_pages_cover_values_conflicts_and_tombstones(store, limit):
    values = []
    for index in range(7):
        operation = mutation(
            store,
            key={
                "kind": "plugin",
                "name": {"ecosystem": "claude", "source": "test", "name": str(index)},
            },
            value=False,
        )
        values.append(store.apply(operation, user_id="alice").entry)
    conflicts = [mutation(store, key=values[0].key, value=True) for _ in range(4)]
    for operation in conflicts:
        store.apply(operation, user_id="alice")
    values[1] = store.apply(
        mutation(store, key=values[1].key, value=None, version=1), user_id="alice"
    ).entry
    entries, alternatives, cursors = [], [], set()
    cursor = None
    while True:
        page = store.read(user_id="alice", limit=limit, cursor=cursor)
        assert page.revision == 12
        assert 0 < len(page.entries) + len(page.conflicts) <= limit
        entries.extend(page.entries)
        alternatives.extend(page.conflicts)
        if page.next_cursor is None:
            break
        assert page.next_cursor not in cursors
        cursors.add(page.next_cursor)
        cursor = page.next_cursor
    assert entries == sorted(values, key=lambda entry: entry.key.storage_key())
    assert alternatives == sorted(conflicts, key=lambda operation: operation.operation_id.hex)


def test_page_cursor_rejects_revision_or_identity_changes(store):
    store.apply(mutation(store), user_id="alice")
    store.apply(mutation(store, value="other"), user_id="alice")
    cursor = store.read(user_id="alice", limit=1).next_cursor
    assert cursor is not None
    with pytest.raises(OmnigentError, match="identity"):
        store.read(user_id="bob", cursor=cursor)
    with workspace_scope(17), pytest.raises(OmnigentError, match="identity"):
        store.read(user_id="alice", cursor=cursor)
    store.apply(mutation(store, value=None, version=1), user_id="alice")
    with pytest.raises(OmnigentError, match="revision changed"):
        store.read(user_id="alice", cursor=cursor)
    assert store.read(user_id="alice", limit=1).revision == 3
    for invalid in ("", "not-a-cursor", "x" * 4097):
        with pytest.raises(OmnigentError, match="cursor"):
            store.read(user_id="alice", cursor=invalid)
    for invalid_limit in (0, 101, True):
        with pytest.raises(OmnigentError, match="page size"):
            store.read(user_id="alice", limit=invalid_limit)


def test_sqlite_page_scan_excludes_receipt_history(tmp_path):
    store = SqlAlchemyPreferenceStore(f"sqlite:///{tmp_path / 'scan.db'}", authority_id="scan")
    for index in range(5):
        store.apply(
            mutation(
                store,
                key={
                    "kind": "plugin",
                    "name": {"ecosystem": "claude", "source": "test", "name": str(index)},
                },
                value=False,
            ),
            user_id="alice",
        )
    counts, plans, connections = [], [], set()

    def progress():
        counts.append(1)
        return 0

    def capture(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith("SELECT"):
            plans.extend(
                connection.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters).all()
            )
            raw = connection.connection.driver_connection
            connections.add(raw)
            raw.set_progress_handler(progress, 1)

    def measured_read():
        counts.clear()
        event.listen(store._engine, "before_cursor_execute", capture)
        try:
            page = store.read(user_id="alice", limit=2)
            assert counts, "Must instrument the connection actually used by the store"
            return page, len(counts)
        finally:
            event.remove(store._engine, "before_cursor_execute", capture)
            for connection in connections:
                connection.set_progress_handler(None, 0)

    before, baseline = measured_read()
    with store._session("seed_receipt_history") as session:
        session.add_all(
            [
                SqlPreference(
                    workspace_id=0,
                    user_id="alice",
                    key=f"shared.v1.receipt.{index:032x}",
                    value="unused receipt payload",
                )
                for index in range(10000)
            ]
        )
    after, steps = measured_read()
    assert after == before
    assert steps <= baseline + 100, (baseline, steps, plans)
    assert any("key>? AND key<?" in row[-1] for row in plans), plans
    assert not any(row[-1].startswith("SCAN preferences") for row in plans), plans


def test_page_never_labels_concurrent_rows_with_an_old_revision(store):
    store.apply(mutation(store), user_id="alice")
    before = store.read(user_id="alice")
    changed = False

    def concurrent_write(connection, cursor, statement, parameters, context, executemany):
        nonlocal changed
        if not changed and "SELECT" in statement and " >= " in statement:
            changed = True
            store.apply(mutation(store, value="newer", version=1), user_id="alice")

    event.listen(store._engine, "after_cursor_execute", concurrent_write)
    try:
        assert store.read(user_id="alice") == before
        assert changed
    finally:
        event.remove(store._engine, "after_cursor_execute", concurrent_write)
    assert store.read(user_id="alice").entries[0].value == "newer"
