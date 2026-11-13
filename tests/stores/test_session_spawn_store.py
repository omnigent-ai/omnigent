"""Pending spawns have no AP target; admission and staged cleanup are exclusive."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier, Event
from uuid import uuid4

import pytest
from sqlalchemy import event, inspect, select, update

from omnigent.db.db_models import (
    SqlConversation,
    SqlConversationMetadata,
    SqlFile,
    SqlSessionSpawn,
    SqlSessionSpawnMaterialization,
    SqlSessionSpawnPayload,
    current_workspace_id,
    workspace_scope,
)
from omnigent.errors import OmnigentError
from omnigent.server.auth import runner_session_in_scope
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.session_spawn_store import (
    PENDING_SPAWN_LIFETIME_SECONDS,
    SessionSpawnStore,
    SpawnActivationCheck,
    SpawnAuthorityError,
    SpawnConflictError,
    SpawnEpochExpiredError,
    SpawnIdentity,
    SpawnPhase,
    SpawnState,
)


def test_spawn_schema_declares_bounded_payloads_and_complete_index_keys():
    from sqlalchemy.dialects.mysql import dialect
    from sqlalchemy.schema import CreateTable

    from omnigent.db.db_models import SqlSessionSpawnEpoch

    for model in (
        SqlSessionSpawn,
        SqlSessionSpawnEpoch,
        SqlSessionSpawnPayload,
        SqlSessionSpawnMaterialization,
    ):
        table = model.__table__
        ddl = str(CreateTable(table).compile(dialect=dialect()))
        assert "BLOB" not in ddl and "TEXT" not in ddl and "DEFAULT" not in ddl
        primary_key = {column.name for column in table.primary_key}
        assert all(
            primary_key <= {column.name for column in index.columns} for index in table.indexes
        )


def _open(conversations, artifacts):
    def current_scope(session, runner_id, parent_id):
        assert session.in_transaction()

        def lookup(session_id):
            scope = conversations.get_runner_scope(session_id)
            if scope is None:
                return None
            metadata = session.scalar(
                select(SqlConversationMetadata)
                .where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversationMetadata.id == session_id,
                )
                .with_for_update()
            )
            if metadata is None:
                return None
            return replace(scope, runner_id=metadata.runner_id)

        return runner_session_in_scope(runner_id, parent_id, lookup)

    return SessionSpawnStore(
        conversations.storage_location,
        encode_payload=lambda value: base64.b64encode(value.encode()).decode(),
        decode_payload=lambda value: base64.b64decode(value).decode(),
        check_current_scope=current_scope,
        artifact_store=artifacts,
    )


@pytest.fixture(params=["conversation_store", "split_db_conversation_store"])
def spawn_context(request):
    conversations = request.getfixturevalue(request.param)
    parent = conversations.create_conversation(runner_id="creator-runner", title="parent")
    identity = SpawnIdentity("creator-runner", parent.id, "owner@example.test")
    return (
        conversations,
        _open(conversations, request.getfixturevalue("artifact_store")),
        SqlAlchemyFileStore(conversations.storage_location),
        identity,
    )


def _reserve(spawns, identity, *, nonce=None, epoch=None, title="worker", files=(), payload=None):
    return spawns.reserve(
        identity,
        nonce or uuid4().hex,
        epoch=epoch if epoch is not None else spawns.current_epoch(identity),
        title=title,
        create_payload=payload if payload is not None else {"sub_agent_name": "worker"},
        source_file_ids=files,
        max_file_bytes=1024,
    )


def test_pending_reservation_has_only_metadata_and_shared_file_rows(spawn_context):
    conversations, spawns, files, identity = spawn_context
    source = files.create("report.txt", 12, session_id=identity.parent_id)
    receipt = _reserve(spawns, identity, files=(source.id,))
    copied = files.get(receipt.file_mapping[source.id], session_id=receipt.child_id)
    assert copied is not None and copied.blob_key == source.blob_key
    assert conversations.get_conversation(receipt.child_id) is None
    with pytest.raises(OmnigentError, match="activation is pending"):
        conversations.create_conversation(parent_conversation_id=receipt.child_id, title="nested")
    with pytest.raises(OmnigentError, match="activation is pending"):
        conversations.create_conversation(side_chat_parent_id=receipt.child_id, title="side chat")
    if conversations._conv_engine is not conversations._engine:
        assert "session_spawns" not in inspect(conversations._conv_engine).get_table_names()


def test_nonce_replay_is_exact_and_name_claim_survives_until_abort(spawn_context):
    conversations, spawns, _, identity = spawn_context
    receipt = _reserve(spawns, identity)
    assert _reserve(spawns, identity, nonce=receipt.nonce, epoch=receipt.epoch) == receipt
    with pytest.raises(SpawnConflictError, match="another request"):
        _reserve(spawns, identity, nonce=receipt.nonce, epoch=receipt.epoch, title="different")
    with pytest.raises(SpawnConflictError, match="name is already reserved"):
        _reserve(spawns, identity)
    assert spawns.abort(identity, receipt.nonce, epoch=receipt.epoch).state == SpawnState.ABORTED
    replacement = _reserve(spawns, identity)
    assert replacement.child_id != receipt.child_id
    assert spawns.abort(identity, receipt.nonce, epoch=receipt.epoch).child_id == receipt.child_id
    assert (
        spawns.get(identity, replacement.nonce, epoch=replacement.epoch).state
        == SpawnState.PENDING
    )
    assert conversations.get_conversation(replacement.child_id) is None


def test_pending_profile_acceptance_requires_activation_claim(spawn_context):
    from omnigent.stores.sandbox_profile_store import SandboxProfileStore

    conversations, spawns, _, identity = spawn_context
    profile = {
        "provider": "company",
        "id": "build",
        "revision": "v1",
        "name": "Build",
        "description": "",
        "user_id": identity.owner_id,
        "workspace_id": current_workspace_id(),
    }
    receipt = _reserve(spawns, identity, payload={"sandbox_profile": profile})
    receipt = spawns.admit(identity, receipt.nonce, {"message": "first"}, epoch=receipt.epoch)
    _materialize(conversations, receipt)
    with pytest.raises(OmnigentError, match="activation is pending"):
        conversations.accept_sandbox_profile(receipt.child_id, profile)
    with pytest.raises(SpawnConflictError, match="matching activation claim"):
        conversations.accept_sandbox_profile(receipt.child_id, profile, _spawn_claim=receipt)
    assert spawns.advance_materialization(receipt, SpawnPhase.MATERIALIZE)
    with pytest.raises(SpawnConflictError, match="matching activation claim"):
        conversations.accept_sandbox_profile(
            receipt.child_id, {**profile, "revision": "v2"}, _spawn_claim=receipt
        )
    conversations.accept_sandbox_profile(receipt.child_id, profile, _spawn_claim=receipt)
    conversations.accept_sandbox_profile(receipt.child_id, profile, _spawn_claim=receipt)
    assert SandboxProfileStore(conversations).get_session_snapshot(receipt.child_id) == profile
    assert spawns.advance_materialization(
        receipt,
        SpawnPhase.ACTIVATE,
        activation_check=SpawnActivationCheck(identity.runner_id, None, None, {}, None),
    )
    with pytest.raises(ValueError, match="cannot be changed"):
        conversations.accept_sandbox_profile(receipt.child_id, {**profile, "revision": "v2"})


@pytest.mark.parametrize("order", ["abort-first", "admit-first", "concurrent"])
@pytest.mark.parametrize("cleanup", ["abort", "expiry"])
def test_abort_and_first_input_cannot_both_win(spawn_context, monkeypatch, order, cleanup):
    from omnigent.stores.session_spawn_store import now_epoch

    _, spawns, files, identity = spawn_context
    source = files.create("source.txt", 4, session_id=identity.parent_id)
    receipt = _reserve(spawns, identity, files=(source.id,))
    first_input = {"type": "message", "data": {"content": "first task"}}
    expires_at = now_epoch() + PENDING_SPAWN_LIFETIME_SECONDS
    if cleanup == "expiry":
        monkeypatch.setattr("omnigent.stores.session_spawn_store.now_epoch", lambda: expires_at)

    def admit():
        try:
            return spawns.admit(identity, receipt.nonce, first_input, epoch=receipt.epoch).state
        except SpawnConflictError:
            return "refused"

    def abort():
        if cleanup == "expiry":
            assert spawns.expire_pending_page()[1] == []
            return spawns.get(identity, receipt.nonce, epoch=receipt.epoch).state
        return spawns.abort(identity, receipt.nonce, epoch=receipt.epoch).state

    if order == "concurrent":
        barrier = Barrier(2)

        def competing(action):
            barrier.wait(timeout=5)
            return action()

        with ThreadPoolExecutor(max_workers=2) as pool:
            admitted = pool.submit(competing, admit)
            aborted = pool.submit(competing, abort)
            results = admitted.result(timeout=10), aborted.result(timeout=10)
    elif order == "abort-first":
        aborted = abort()
        results = admit(), aborted
    else:
        results = admit(), abort()
    assert results in (
        ("refused", SpawnState.ABORTED),
        (SpawnState.ADMITTED, SpawnState.ADMITTED),
    )
    final = spawns.get(identity, receipt.nonce, epoch=receipt.epoch)
    assert final.input_payload == (first_input if final.state == SpawnState.ADMITTED else None)
    assert (files.get(receipt.file_mapping[source.id]) is not None) == (
        final.state == SpawnState.ADMITTED
    )
    assert files.get(source.id) is not None


def test_admission_payload_survives_restart_and_each_phase_is_idempotent(spawn_context):
    conversations, spawns, _, identity = spawn_context
    receipt = _reserve(spawns, identity)
    payload = {"message": "private input sentinel"}
    spawns.admit(identity, receipt.nonce, payload, epoch=receipt.epoch)
    restarted = _open(conversations, spawns._artifacts)
    work = restarted.list_work(SpawnState.ADMITTED, SpawnPhase.MATERIALIZE, limit=1)
    assert len(work) == 1 and work[0].input_payload == payload
    with spawns._session("inspect_encoded_spawn_input") as session:
        stored = session.scalar(select(SqlSessionSpawn.input_payload))
        assert stored is not None
    encoded = spawns._artifacts.get(spawns._artifact_key(stored))
    assert b"private input sentinel" not in encoded
    with pytest.raises(SpawnConflictError, match="different first input"):
        restarted.admit(identity, receipt.nonce, {"message": "changed"}, epoch=receipt.epoch)
    _materialize(conversations, work[0])
    assert restarted.advance_materialization(receipt, SpawnPhase.MATERIALIZE)
    assert not restarted.advance_materialization(receipt, SpawnPhase.MATERIALIZE)
    with pytest.raises(SpawnConflictError, match="explicit validated child state"):
        restarted.advance_materialization(receipt, SpawnPhase.ACTIVATE)
    assert restarted.advance_materialization(
        receipt,
        SpawnPhase.ACTIVATE,
        activation_check=SpawnActivationCheck(identity.runner_id, None, None, {}, None),
    )
    assert restarted.advance_materialization(receipt, SpawnPhase.DISPATCH)
    retried = restarted.admit(identity, receipt.nonce, payload, epoch=receipt.epoch)
    assert retried.phase == SpawnPhase.READY and retried.input_payload is None
    assert (
        restarted.abort(identity, receipt.nonce, epoch=receipt.epoch).state == SpawnState.ADMITTED
    )


@pytest.mark.parametrize("legacy_reference", [False, True])
def test_orphan_blob_cleanup_receipt_is_durable_and_shared_parent_blob_survives(
    spawn_context, legacy_reference
):
    conversations, spawns, files, identity = spawn_context
    shared = files.create("shared.txt", 4, session_id=identity.parent_id)
    if legacy_reference:
        with spawns._session("prepare_legacy_file_reference") as session:
            session.execute(
                update(SqlFile)
                .where(
                    SqlFile.workspace_id == current_workspace_id(),
                    SqlFile.id == shared.id,
                )
                .values(blob_key=None)
            )
    orphan = files.create("orphan.txt", 4, session_id=identity.parent_id)
    receipt = _reserve(spawns, identity, files=(shared.id, orphan.id))
    files.delete(orphan.id, session_id=identity.parent_id)
    reference_statements = []
    reference_plans = []

    def observe_reference_query(connection, cursor, statement, parameters, context, many):
        if (
            not statement.lstrip().startswith("SELECT")
            or "FROM files" not in statement
            or "files.session_id" in statement
        ):
            return
        reference_statements.append(statement)
        if connection.dialect.name == "sqlite":
            reference_plans.append(
                " ".join(
                    row[3]
                    for row in connection.exec_driver_sql(
                        "EXPLAIN QUERY PLAN " + statement, parameters
                    )
                )
            )

    event.listen(spawns._engine, "before_cursor_execute", observe_reference_query)
    try:
        aborted = spawns.abort(identity, receipt.nonce, epoch=receipt.epoch)
    finally:
        event.remove(spawns._engine, "before_cursor_execute", observe_reference_query)
    # Each blob's modern and legacy references share one statement snapshot.
    assert len(reference_statements) == len(receipt.file_mapping)
    for plan in reference_plans:
        assert "ix_files_blob_reference (workspace_id=? AND blob_key=?)" in plan, plan
        assert "sqlite_autoindex_files_1 (workspace_id=? AND id=?)" in plan, plan
        assert "SCAN files" not in plan, plan
    assert aborted.state == SpawnState.ABORTING
    assert aborted.cleanup_blob_keys == (orphan.blob_key,)
    assert files.get(shared.id) is not None
    restarted = _open(conversations, spawns._artifacts)
    assert restarted.list_work(SpawnState.ABORTING, SpawnPhase.CLEANUP) == [aborted]
    restarted.finish_abort(receipt.child_id, aborted.cleanup_blob_keys)
    restarted.finish_abort(receipt.child_id, aborted.cleanup_blob_keys)
    assert restarted.get(identity, receipt.nonce, epoch=receipt.epoch).state == SpawnState.ABORTED


def test_epoch_rollover_retains_active_attempts_and_never_recreates_retired_nonce(
    spawn_context, monkeypatch
):
    _, spawns, _, identity = spawn_context
    monkeypatch.setattr("omnigent.stores.session_spawn_store.MAX_TERMINAL_RECEIPTS_PER_PARENT", 2)
    active = _reserve(spawns, identity, title="active")
    retired = []
    for title in ("finished-one", "finished-two"):
        receipt = _reserve(spawns, identity, title=title)
        spawns.abort(identity, receipt.nonce, epoch=receipt.epoch)
        retired.append(receipt)
    epoch = spawns.current_epoch(identity)
    assert epoch > active.epoch
    assert (
        _reserve(spawns, identity, nonce=active.nonce, epoch=active.epoch, title="active")
        == active
    )
    for receipt in retired:
        assert spawns.get(identity, receipt.nonce, epoch=receipt.epoch) is None
        assert spawns.abort(identity, receipt.nonce, epoch=receipt.epoch) is None
        with pytest.raises(SpawnEpochExpiredError):
            _reserve(spawns, identity, nonce=receipt.nonce, epoch=receipt.epoch, title="fresh")
    fresh = _reserve(spawns, identity, epoch=epoch, title="finished-one")
    assert fresh.child_id not in {receipt.child_id for receipt in retired}


def test_authority_checks_inherited_scope_and_rechecks_rebinding(spawn_context):
    conversations, spawns, _, identity = spawn_context
    child = conversations.create_conversation(
        parent_conversation_id=identity.parent_id, title="unbound"
    )
    inherited = replace(identity, parent_id=child.id)
    receipt = _reserve(spawns, inherited)
    conversations.set_runner_id(child.id, "different-runner")
    for operation in (
        lambda: spawns.abort(inherited, receipt.nonce, epoch=receipt.epoch),
        lambda: spawns.admit(inherited, receipt.nonce, {"message": "task"}, epoch=receipt.epoch),
    ):
        with pytest.raises(SpawnAuthorityError):
            operation()
    assert spawns.list_work(SpawnState.PENDING, SpawnPhase.PREPARED)[0].state == SpawnState.PENDING


def test_unknown_attempt_and_wrong_owner_cannot_clean_another_reservation(spawn_context):
    _, spawns, _, identity = spawn_context
    receipt = _reserve(spawns, identity)
    assert spawns.abort(identity, uuid4().hex, epoch=receipt.epoch) is None
    with pytest.raises(SpawnAuthorityError):
        spawns.abort(
            replace(identity, owner_id="other@example.test"), receipt.nonce, epoch=receipt.epoch
        )
    assert spawns.get(identity, receipt.nonce, epoch=receipt.epoch).state == SpawnState.PENDING


def test_preparation_bounds_are_atomic_and_active_limit_is_not_a_lifetime_limit(
    spawn_context, monkeypatch
):
    _, spawns, files, identity = spawn_context
    source = files.create("too-large.txt", 2048, session_id=identity.parent_id)
    with pytest.raises(ValueError, match="nonempty"):
        spawns.reserve(identity, "", epoch=1, title="worker", create_payload={}, max_file_bytes=0)
    with pytest.raises(SpawnConflictError, match="byte limit"):
        _reserve(spawns, identity, files=(source.id,))
    with pytest.raises(ValueError, match="byte limit"):
        _reserve(spawns, identity, payload={"large": "x" * 65536})
    assert not spawns.list_work(SpawnState.PENDING, SpawnPhase.PREPARED)
    monkeypatch.setattr("omnigent.stores.session_spawn_store.MAX_PENDING_PER_PARENT", 1)
    receipt = _reserve(spawns, identity)
    with pytest.raises(SpawnConflictError, match="active spawn limit"):
        _reserve(spawns, identity, title="other")
    spawns.abort(identity, receipt.nonce, epoch=receipt.epoch)
    assert _reserve(spawns, identity, title="other").state == SpawnState.PENDING


def test_unknown_payload_version_is_not_interpreted_as_a_spawn_request(spawn_context):
    _, spawns, _, identity = spawn_context
    receipt = _reserve(spawns, identity)
    with spawns._write("replace_test_payload_version") as session:
        row = session.scalar(
            select(SqlSessionSpawn).where(SqlSessionSpawn.child_id == receipt.child_id)
        )
        payload = session.get(SqlSessionSpawnPayload, (current_workspace_id(), row.create_payload))
        encoded = spawns._encode(json.dumps({"version": 99, "data": {}})).encode()
        payload.encoded_digest = hashlib.sha256(encoded).digest()
        payload_id = payload.id
    spawns._artifacts.put(spawns._artifact_key(payload_id), encoded)
    with pytest.raises(SpawnConflictError, match="payload version"):
        spawns.get(identity, receipt.nonce, epoch=receipt.epoch)
    # Cleanup does not need to interpret a pending request's opaque payload.
    assert spawns.abort(identity, receipt.nonce, epoch=receipt.epoch).state == SpawnState.ABORTED


@pytest.mark.parametrize("upload", ["prepare", "input"])
@pytest.mark.parametrize("cleanup", ["abort", "expiry"])
def test_payload_cleanup_retains_delayed_upload_fence(spawn_context, monkeypatch, upload, cleanup):
    from omnigent.stores.session_spawn_store import now_epoch

    conversations, spawns, _, identity = spawn_context
    created_at = now_epoch()
    expires_at = created_at + PENDING_SPAWN_LIFETIME_SECONDS
    monkeypatch.setattr("omnigent.stores.session_spawn_store.now_epoch", lambda: created_at)
    epoch, nonce = spawns.current_epoch(identity), uuid4().hex
    if upload == "input":
        _reserve(spawns, identity, nonce=nonce, epoch=epoch)
    uploading, release = Event(), Event()
    uploaded_keys = []
    put = spawns._artifacts.put

    def delayed_put(key, data):
        uploaded_keys.append(key)
        uploading.set()
        assert release.wait(timeout=10)
        put(key, data)

    monkeypatch.setattr(spawns._artifacts, "put", delayed_put)

    def write():
        if upload == "prepare":
            return _reserve(spawns, identity, nonce=nonce, epoch=epoch)
        return spawns.admit(identity, nonce, {"message": "first"}, epoch=epoch)

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(write)
        try:
            assert uploading.wait(timeout=10)
            # These writes must complete while artifact I/O is paused.
            if cleanup == "expiry":
                monkeypatch.setattr(
                    "omnigent.stores.session_spawn_store.now_epoch", lambda: expires_at
                )
                assert spawns.expire_pending_page()[1] == []
                aborted = spawns.get(identity, nonce, epoch=epoch)
            else:
                aborted = spawns.abort(identity, nonce, epoch=epoch)
            assert aborted.state == SpawnState.ABORTED
            assert spawns.cleanup_payloads() == []
            assert not spawns._artifacts.exists(uploaded_keys[0])
        finally:
            release.set()
        if upload == "input":
            with pytest.raises(SpawnConflictError, match="aborted"):
                result.result(timeout=10)
        else:
            assert result.result(timeout=10).state == SpawnState.ABORTED
    assert conversations.get_conversation(aborted.child_id) is None
    assert spawns._artifacts.exists(uploaded_keys[0])
    with spawns._session("inspect_due_test_payload_cleanup") as session:
        due = session.scalar(
            select(SqlSessionSpawnPayload.cleanup_after).where(
                SqlSessionSpawnPayload.id == uploaded_keys[0].rsplit("/", 1)[1]
            )
        )
    # Restarted cleanup does not need a live receipt or uploader task.
    restarted = _open(conversations, spawns._artifacts)
    monkeypatch.setattr("omnigent.stores.session_spawn_store.now_epoch", lambda: due)
    assert restarted.cleanup_payloads() == []
    assert not spawns._artifacts.exists(uploaded_keys[0])
    with spawns._session("inspect_retained_test_payload_cleanup") as session:
        payload = session.get(
            SqlSessionSpawnPayload,
            (current_workspace_id(), uploaded_keys[0].rsplit("/", 1)[1]),
        )
        assert payload is not None and payload.cleanup_after > due


@pytest.mark.parametrize("revocation", ["parent", "account"])
async def test_global_pending_expiry_is_bounded_and_survives_revocation(
    spawn_context, monkeypatch, revocation
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from omnigent.db.account_authority import account_authority_scope
    from omnigent.db.db_models import SqlUser
    from omnigent.server.session_spawns import SessionSpawnCoordinator, SessionSpawnRecovery

    conversations, spawns, files, _ = spawn_context
    clock = [1_800_000_000]
    monkeypatch.setattr("omnigent.stores.session_spawn_store.now_epoch", lambda: clock[0])
    receipts = []
    for workspace_id in (7, 11):
        with workspace_scope(workspace_id):
            parent = conversations.create_conversation(runner_id="creator-runner")
            identity = SpawnIdentity("creator-runner", parent.id, "owner@example.test")
            shared = files.create("shared.txt", 4, session_id=parent.id)
            orphan = files.create("orphan.txt", 4, session_id=parent.id)
            spawns._artifacts.put(shared.blob_key, b"keep")
            spawns._artifacts.put(orphan.blob_key, b"drop")
            receipt = _reserve(spawns, identity, files=(shared.id, orphan.id))
            files.delete(orphan.id, session_id=parent.id)
            admitted = _reserve(spawns, identity, title="admitted")
            spawns.admit(identity, admitted.nonce, {"first": True}, epoch=admitted.epoch)
            clock[0] += 1
            assert (
                _reserve(
                    spawns,
                    identity,
                    nonce=receipt.nonce,
                    epoch=receipt.epoch,
                    files=(shared.id, orphan.id),
                ).state
                == SpawnState.PENDING
            )
            if revocation == "parent":
                conversations.replace_runner_id(
                    parent.id, "replacement-runner", expected_runner_id="creator-runner"
                )
            else:
                with spawns._write("revoke_expiry_test_owner") as session:
                    session.add(SqlUser(id=identity.owner_id, deleted_at=clock[0]))
            with pytest.raises((SpawnAuthorityError, OmnigentError)):
                spawns.abort(identity, receipt.nonce, epoch=receipt.epoch)
            receipts.append((workspace_id, receipt, shared, orphan, admitted))

    clock[0] = 1_800_000_000 + PENDING_SPAWN_LIFETIME_SECONDS - 1
    assert spawns.expire_pending_page(limit=1) == (None, [])
    clock[0] += 1
    cursor, failures = spawns.expire_pending_page(limit=1)
    assert cursor is not None and failures == []
    with workspace_scope(11):
        assert spawns.get_claimed_cleanup(receipts[1][1].child_id) is None
    clock[0] += 1
    # Recovery carries a stale account context, which expiry must not borrow.
    with account_authority_scope("owner@example.test", "revoked-generation"):
        cursor, failures = spawns.expire_pending_page(after=cursor, limit=1)
    assert cursor is not None and failures == []
    assert spawns.expire_pending_page(after=cursor, limit=1) == (None, [])

    for workspace_id, receipt, shared, orphan, admitted in receipts:
        with workspace_scope(workspace_id):
            expired = spawns.get_claimed_cleanup(receipt.child_id)
            assert expired.state == SpawnState.ABORTING
            assert expired.cleanup_blob_keys == (orphan.blob_key,)
            assert files.get(shared.id) is not None
            assert spawns._artifacts.get(shared.blob_key) == b"keep"
            assert files.list(session_id=receipt.child_id).data == []
            assert conversations.get_conversation(receipt.child_id) is None
            assert not spawns._expire_pending(admitted.child_id, clock[0])
            assert not spawns._expire_pending(uuid4().hex, clock[0])
            with spawns._session("inspect_expiry_claims") as session:
                row = session.scalar(
                    select(SqlSessionSpawn).where(SqlSessionSpawn.child_id == receipt.child_id)
                )
                assert row.name_digest is None and row.create_payload is None
                payloads = list(
                    session.scalars(
                        select(SqlSessionSpawnPayload).where(
                            SqlSessionSpawnPayload.child_id == receipt.child_id
                        )
                    )
                )
                assert payloads and all(row.cleanup_after is not None for row in payloads)
    coordinator = SessionSpawnCoordinator(
        conversations,
        AsyncMock(),
        activate=AsyncMock(),
        cleanup_blob=spawns._artifacts.delete,
        artifact_store=spawns._artifacts,
    )
    coordinator.spawns = spawns
    assert await coordinator.recover_global_page(
        SimpleNamespace(), SpawnState.ABORTING, SpawnPhase.CLEANUP
    ) == (None, [])
    monkeypatch.setattr(coordinator, "recover_global_page", AsyncMock(return_value=(None, [])))
    recovery = SessionSpawnRecovery(coordinator)
    await recovery.recover_once(SimpleNamespace())
    for workspace_id, receipt, shared, orphan, admitted in receipts:
        with workspace_scope(workspace_id):
            assert spawns.get_claimed_cleanup(receipt.child_id).state == SpawnState.ABORTED
            assert not spawns._artifacts.exists(orphan.blob_key)
            assert spawns._artifacts.get(shared.blob_key) == b"keep"


def test_large_payload_admission_retries_after_lost_artifact_response(spawn_context, monkeypatch):
    conversations, spawns, _, identity = spawn_context
    create = {"instructions": "large create " * 3000}
    first = {"message": "large input " * 80000}
    receipt = _reserve(spawns, identity, payload=create)
    put = spawns._artifacts.put
    written = []

    def lose_response(key, data):
        put(key, data)
        written.append(key)
        raise OSError("artifact response lost")

    monkeypatch.setattr(spawns._artifacts, "put", lose_response)
    with pytest.raises(OSError, match="response lost"):
        spawns.admit(identity, receipt.nonce, first, epoch=receipt.epoch)
    restarted = _open(conversations, spawns._artifacts)
    pending = restarted.get(identity, receipt.nonce, epoch=receipt.epoch)
    assert pending.state == SpawnState.PENDING and pending.input_payload == first
    assert conversations.get_conversation(receipt.child_id) is None
    monkeypatch.setattr(spawns._artifacts, "put", put)
    admitted = restarted.admit(identity, receipt.nonce, first, epoch=receipt.epoch)
    assert admitted.state == SpawnState.ADMITTED
    assert admitted.create_payload == create and admitted.input_payload == first
    assert restarted.cleanup_payloads() == []
    assert not spawns._artifacts.exists(written[0])
    assert restarted.get(identity, receipt.nonce, epoch=receipt.epoch) == admitted
    from omnigent.stores.conversation_store import sqlalchemy_store

    run_write = sqlalchemy_store.run_write_transaction

    def fail_metadata(factory, operation, callback):
        if operation == "insert_conversation_metadata":
            raise RuntimeError("metadata temporarily unavailable")
        return run_write(factory, operation, callback)

    with monkeypatch.context() as fail:
        fail.setattr(sqlalchemy_store, "run_write_transaction", fail_metadata)
        with pytest.raises(RuntimeError, match="metadata temporarily unavailable"):
            _materialize(conversations, admitted, text=first["message"])
    _materialize(conversations, admitted, text=first["message"])
    [stored_first] = conversations.list_pending_inputs(receipt.child_id)
    assert stored_first.data.content[0]["text"] == first["message"]
    with spawns._session("inspect_small_test_payload_references") as session:
        row = session.scalar(
            select(SqlSessionSpawn).where(SqlSessionSpawn.child_id == receipt.child_id)
        )
        assert (
            len(bytes.fromhex(row.create_payload)) == len(bytes.fromhex(row.input_payload)) == 16
        )
        payload = session.get(SqlSessionSpawnPayload, (current_workspace_id(), row.input_payload))
        current_key = spawns._artifact_key(payload.id)
    spawns._artifacts.put(current_key, b"invalid opaque data")
    with pytest.raises(SpawnConflictError, match="integrity"):
        restarted.get(identity, receipt.nonce, epoch=receipt.epoch)


def test_source_delete_and_staged_copy_preserve_blob_references(spawn_context):
    _, spawns, files, identity = spawn_context
    source = files.create("shared.txt", 4, session_id=identity.parent_id)
    epoch = spawns.current_epoch(identity)
    barrier = Barrier(2)

    def copy():
        barrier.wait(timeout=5)
        try:
            return _reserve(spawns, identity, epoch=epoch, files=(source.id,))
        except SpawnConflictError:
            return None

    def remove_source():
        barrier.wait(timeout=5)
        files.delete(source.id, session_id=identity.parent_id)
        return files.is_blob_key_orphaned(source.blob_key)

    with ThreadPoolExecutor(max_workers=2) as pool:
        prepared = pool.submit(copy)
        removed = pool.submit(remove_source)
        receipt, orphaned = prepared.result(timeout=10), removed.result(timeout=10)
    assert orphaned is (receipt is None)
    if receipt is not None:
        assert files.get(receipt.file_mapping[source.id]).blob_key == source.blob_key


@pytest.mark.asyncio
async def test_global_recovery_restores_workspace_before_payload_reads(spawn_context):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from omnigent.entities.conversation import MessageData, NewConversationItem
    from omnigent.server.host_registry import HostRegistry
    from omnigent.server.session_spawns import SessionSpawnCoordinator, SessionSpawnRecovery
    from omnigent.server.startup_inputs import (
        STARTUP_LABEL,
        ManagedStartupInputs,
        initial_input_id,
    )
    from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID

    conversations, original, files, _ = spawn_context
    startup = ManagedStartupInputs(conversations, HostRegistry())
    coordinator = SessionSpawnCoordinator(
        conversations,
        startup,
        activate=AsyncMock(
            side_effect=lambda receipt, child: SpawnActivationCheck(
                child.runner_id, child.host_id, child.workspace, {}, None
            )
        ),
        cleanup_blob=original._artifacts.delete,
        artifact_store=original._artifacts,
    )
    spawns = coordinator.spawns
    receipts = {}
    cleanup_keys = []
    for workspace_id in (7, 11):
        with workspace_scope(workspace_id):
            parent = conversations.create_conversation(title="parent", runner_id="creator")
            identity = SpawnIdentity("creator", parent.id, "owner@example.test")
            receipt = _reserve(
                spawns,
                identity,
                payload={
                    "arguments": {
                        "title": "worker",
                        "parent_conversation_id": parent.id,
                        "runner_id": "creator",
                        "kind": "sub_agent",
                        "sub_agent_name": "worker",
                        "labels": {STARTUP_LABEL: "pending"},
                    }
                },
            )
            first = NewConversationItem(
                stable_id=initial_input_id(receipt.child_id),
                type="message",
                status="in_progress",
                response_id=PENDING_INPUT_RESPONSE_ID,
                created_by=identity.owner_id,
                data=MessageData(
                    role="user", content=[{"type": "input_text", "text": str(workspace_id)}]
                ),
            )
            receipts[workspace_id] = spawns.admit(
                identity, receipt.nonce, first.model_dump(mode="json"), epoch=receipt.epoch
            )
            aborted = _reserve(spawns, identity, title="abort-me")
            with spawns._session("inspect_workspace_cleanup_reference") as session:
                payload_id = session.scalar(
                    select(SqlSessionSpawn.create_payload).where(
                        SqlSessionSpawn.child_id == aborted.child_id
                    )
                )
            cleanup_keys.append(spawns._artifact_key(payload_id))
            spawns.abort(identity, aborted.nonce, epoch=aborted.epoch)

    decoded_in = []
    decode = spawns._decode

    def record_decode(value):
        decoded_in.append(current_workspace_id())
        return decode(value)

    spawns._decode = record_decode
    assert len(spawns.list_global_work(SpawnState.ADMITTED, SpawnPhase.MATERIALIZE, limit=1)) == 1
    assert decoded_in == []
    cursor = None
    for _ in range(3):
        cursor, failures = await coordinator.recover_global_page(
            SimpleNamespace(), SpawnState.ADMITTED, SpawnPhase.MATERIALIZE, after=cursor, limit=1
        )
        assert failures == [] and current_workspace_id() == 0
        if cursor is None:
            break
    assert cursor is None and set(decoded_in) == {7, 11}
    for workspace_id, receipt in receipts.items():
        with workspace_scope(workspace_id):
            [first] = conversations.list_pending_inputs(receipt.child_id)
            assert first.data.content[0]["text"] == str(workspace_id)
    assert spawns.cleanup_payloads(limit=1, all_workspaces=True) == []
    assert sum(spawns._artifacts.exists(key) for key in cleanup_keys) == 1
    assert spawns.cleanup_payloads(limit=1, all_workspaces=True) == []
    assert not any(spawns._artifacts.exists(key) for key in cleanup_keys)
    assert current_workspace_id() == 0

    dispatched = []
    delivered = asyncio.Event()

    async def dispatch(context, child_id, event):
        assert await context.claim(conversations, child_id)
        dispatched.append((current_workspace_id(), child_id))
        if len(dispatched) == len(receipts):
            delivered.set()
        return {"accepted": True}

    startup.dispatch = dispatch
    recovery = SessionSpawnRecovery(coordinator, interval_seconds=0.01)
    recovery.start(SimpleNamespace())
    try:
        await asyncio.wait_for(delivered.wait(), timeout=10)
    finally:
        await recovery.shutdown()
    await recovery.recover_once(SimpleNamespace())
    await recovery.recover_once(SimpleNamespace())
    assert sorted(dispatched) == sorted(
        (workspace_id, receipt.child_id) for workspace_id, receipt in receipts.items()
    )
    assert current_workspace_id() == 0

    with workspace_scope(11):
        identity = receipts[11].identity
        source = files.create("orphan.txt", 4, session_id=identity.parent_id)
        spawns._artifacts.put(source.blob_key or source.id, b"data")
        pending = _reserve(spawns, identity, title="orphaned", files=(source.id,))
        files.delete(source.id, session_id=identity.parent_id)
        aborted = spawns.abort(identity, pending.nonce, epoch=pending.epoch)
        assert aborted.state == SpawnState.ABORTING
        conversations.clear_runner_id(identity.parent_id)
        conversations.set_runner_id(identity.parent_id, "replacement")
    _, failures = await coordinator.recover_global_page(
        SimpleNamespace(), SpawnState.ABORTING, SpawnPhase.CLEANUP, limit=1
    )
    assert failures == []
    with workspace_scope(11):
        assert spawns.get_claimed_cleanup(pending.child_id).state == SpawnState.ABORTED
        assert not spawns._artifacts.exists(source.blob_key or source.id)
    await startup.shutdown()


def _materialize(conversations, receipt, *, text="first"):
    from omnigent.entities.conversation import MessageData, NewConversationItem
    from omnigent.server.startup_inputs import STARTUP_LABEL, initial_input_id
    from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID

    return conversations.create_conversation(
        conversation_id=receipt.child_id,
        parent_conversation_id=receipt.identity.parent_id,
        title="worker",
        kind="sub_agent",
        sub_agent_name="worker",
        runner_id=receipt.identity.runner_id,
        labels={STARTUP_LABEL: "pending"},
        _spawn_claim=receipt,
        _spawn_first_input=NewConversationItem(
            stable_id=initial_input_id(receipt.child_id),
            type="message",
            status="in_progress",
            response_id=PENDING_INPUT_RESPONSE_ID,
            created_by=receipt.identity.owner_id,
            data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
        ),
    )


def test_pending_id_is_not_available_to_any_caller_supplied_id_creation(spawn_context):
    conversations, spawns, _, identity = spawn_context
    receipt = _reserve(spawns, identity)
    creators = (
        lambda: conversations.create_conversation(conversation_id=receipt.child_id),
        lambda: conversations._create_session_with_agent_with_id(
            receipt.child_id,
            agent_id=uuid4().hex,
            agent_name="worker",
            agent_bundle_location="unused",
            agent_description=None,
        ),
        lambda: conversations._fork_conversation_with_id(receipt.child_id, identity.parent_id),
        lambda: _materialize(conversations, receipt),
    )
    for create in creators:
        with pytest.raises(SpawnConflictError):
            create()
    assert conversations.get_conversation(receipt.child_id) is None


def test_materialization_recovers_after_ap_commit_without_replacing_metadata(
    spawn_context, monkeypatch
):
    from omnigent.stores.conversation_store import sqlalchemy_store

    conversations, spawns, _, identity = spawn_context
    receipt = _reserve(spawns, identity)
    admitted = spawns.admit(identity, receipt.nonce, {"message": "first"}, epoch=receipt.epoch)
    run = sqlalchemy_store.run_write_transaction

    def fail_metadata(factory, operation, callback):
        if operation == "insert_conversation_metadata":
            raise RuntimeError("simulated interruption after AP commit")
        return run(factory, operation, callback)

    with monkeypatch.context() as faults:
        faults.setattr(sqlalchemy_store, "run_write_transaction", fail_metadata)
        with pytest.raises(RuntimeError, match="after AP commit"):
            _materialize(conversations, admitted)
    with conversations._conv_session("inspect_materialized_child") as session:
        assert session.get(SqlConversation, (current_workspace_id(), receipt.child_id)) is not None
        assert (
            session.get(SqlSessionSpawnMaterialization, (current_workspace_id(), receipt.child_id))
            is not None
        )
    with conversations._session("inspect_absent_child_metadata") as session:
        assert (
            session.get(SqlConversationMetadata, (current_workspace_id(), receipt.child_id))
            is None
        )
    [first] = conversations.list_pending_inputs(receipt.child_id)
    assert first.status == "in_progress" and first.created_by == identity.owner_id
    recovered = _materialize(conversations, admitted)
    assert recovered.id == receipt.child_id and recovered.runner_id == identity.runner_id
    conversations.set_labels(receipt.child_id, {"latest": "retained"})
    assert _materialize(conversations, admitted).labels["latest"] == "retained"
    assert spawns.get(identity, receipt.nonce, epoch=receipt.epoch).input_payload == {
        "message": "first"
    }


def test_materialization_retry_preserves_cancelled_first_input(spawn_context):
    from omnigent.server.host_registry import HostRegistry
    from omnigent.server.startup_inputs import STARTUP_LABEL, ManagedStartupInputs

    conversations, spawns, _, identity = spawn_context
    receipt = _reserve(spawns, identity)
    admitted = spawns.admit(identity, receipt.nonce, {"message": "first"}, epoch=receipt.epoch)
    _materialize(conversations, admitted)
    [first] = conversations.list_pending_inputs(receipt.child_id)
    startup = ManagedStartupInputs(conversations, HostRegistry())
    asyncio.run(startup.cancel(receipt.child_id))
    recovered = _materialize(conversations, admitted)
    assert recovered.labels[STARTUP_LABEL] == "cancelled"
    assert conversations.list_pending_inputs(receipt.child_id) == []
    assert conversations.get_item(receipt.child_id, first.id) is None


def test_pending_child_rejects_ordinary_setting_and_binding_mutations(spawn_context):
    conversations, spawns, _, identity = spawn_context
    receipt = _reserve(spawns, identity)
    admitted = spawns.admit(identity, receipt.nonce, {"message": "first"}, epoch=receipt.epoch)
    child = _materialize(conversations, admitted)
    for mutate in (
        lambda: conversations.update_conversation(child.id, title="changed"),
        lambda: conversations.set_runner_id(child.id, "replacement"),
        lambda: conversations.replace_runner_id(child.id, "replacement"),
        lambda: conversations.clear_runner_id(child.id),
        lambda: conversations.set_host_id(child.id, uuid4().hex, workspace="/different"),
        lambda: conversations.clear_host_binding(child.id),
    ):
        with pytest.raises(OmnigentError, match="activation is pending"):
            mutate()
    assert conversations.get_conversation(child.id) == child


@pytest.mark.parametrize("status", ["incomplete", "failed"])
def test_materialization_retry_never_resets_first_input_status(spawn_context, status):
    from omnigent.server.startup_inputs import STARTUP_LABEL

    conversations, spawns, _, identity = spawn_context
    receipt = _reserve(spawns, identity)
    admitted = spawns.admit(identity, receipt.nonce, {"message": "first"}, epoch=receipt.epoch)
    _materialize(conversations, admitted)
    [first] = conversations.list_pending_inputs(receipt.child_id)
    assert conversations.transition_pending_input(
        receipt.child_id, first.id, expected="in_progress", status=status
    )
    conversations.delete_label(receipt.child_id, STARTUP_LABEL)
    recovered = _materialize(conversations, admitted)
    assert STARTUP_LABEL not in recovered.labels
    [after] = conversations.list_pending_inputs(receipt.child_id)
    assert after.id == first.id and after.status == status


def test_deleted_materialization_is_never_recreated_by_delayed_replay(spawn_context):
    conversations, spawns, _, identity = spawn_context
    receipt = _reserve(spawns, identity)
    admitted = spawns.admit(identity, receipt.nonce, {"message": "first"}, epoch=receipt.epoch)
    _materialize(conversations, admitted)
    assert not conversations.spawn_was_deleted(admitted)
    assert asyncio.run(conversations.delete_conversation(receipt.child_id))
    assert conversations.spawn_was_deleted(admitted)
    assert not conversations.spawn_was_deleted(replace(admitted, nonce=uuid4().hex))
    with pytest.raises(SpawnConflictError, match="was deleted"):
        _materialize(conversations, admitted)
    with pytest.raises(SpawnConflictError):
        conversations.create_conversation(conversation_id=receipt.child_id)
    assert conversations.get_conversation(receipt.child_id) is None
    assert spawns.abort(identity, receipt.nonce, epoch=receipt.epoch).state == SpawnState.ADMITTED
    from unittest.mock import AsyncMock

    from omnigent.server.host_registry import HostRegistry
    from omnigent.server.session_spawns import SessionSpawnCoordinator
    from omnigent.server.startup_inputs import ManagedStartupInputs

    startup = ManagedStartupInputs(conversations, HostRegistry())
    activation = AsyncMock()
    coordinator = SessionSpawnCoordinator(
        conversations,
        startup,
        activate=activation,
        cleanup_blob=spawns._artifacts.delete,
        artifact_store=spawns._artifacts,
    )
    conversations.clear_runner_id(identity.parent_id)
    conversations.set_runner_id(identity.parent_id, "replacement-runner")
    _, failures = asyncio.run(
        coordinator.recover_global_page(None, SpawnState.ADMITTED, SpawnPhase.MATERIALIZE)
    )
    assert not failures
    activation.assert_not_awaited()
    with spawns._session("inspect_deleted_spawn_retirement") as session:
        row = session.scalar(
            select(SqlSessionSpawn).where(SqlSessionSpawn.child_id == receipt.child_id)
        )
        assert row.phase == SpawnPhase.READY
        assert row.create_payload is None and row.input_payload is None
        assert row.name_digest is None
    with pytest.raises(SpawnConflictError):
        _materialize(conversations, admitted)
    assert conversations.get_conversation(receipt.child_id) is None


def test_ordinary_name_collision_does_not_adopt_or_replace_existing_child(spawn_context):
    conversations, spawns, _, identity = spawn_context
    receipt = _reserve(spawns, identity)
    ordinary = conversations.create_conversation(
        parent_conversation_id=identity.parent_id, title="worker"
    )
    admitted = spawns.admit(identity, receipt.nonce, {"message": "first"}, epoch=receipt.epoch)
    from omnigent.stores.conversation_store import NameAlreadyExistsError

    with pytest.raises(NameAlreadyExistsError):
        _materialize(conversations, admitted)
    assert conversations.get_conversation(ordinary.id) is not None
    assert conversations.get_conversation(receipt.child_id) is None
    with conversations._conv_session("inspect_uncommitted_spawn_marker") as session:
        assert (
            session.get(SqlSessionSpawnMaterialization, (current_workspace_id(), receipt.child_id))
            is None
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_before_activation", [False, True])
async def test_existing_startup_worker_waits_for_durable_child_activation(
    spawn_context, cancel_before_activation, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from omnigent.server.host_registry import HostRegistry
    from omnigent.server.startup_inputs import ManagedStartupInputs

    conversations, spawns, _, identity = spawn_context
    receipt = _reserve(spawns, identity)
    admitted = spawns.admit(identity, receipt.nonce, {"message": "first"}, epoch=receipt.epoch)
    _materialize(conversations, admitted)
    host_id = uuid4().hex
    with conversations._session_immediate("bind_test_spawn_host") as session:
        metadata = session.get(SqlConversationMetadata, (current_workspace_id(), receipt.child_id))
        metadata.host_id = host_id
        metadata.workspace = "/workspace"
    assert spawns.advance_materialization(receipt, SpawnPhase.MATERIALIZE)
    [first] = conversations.list_pending_inputs(receipt.child_id)
    startup = ManagedStartupInputs(conversations, HostRegistry())
    startup.dispatch = AsyncMock()
    deliver = AsyncMock()
    monkeypatch.setattr(startup, "_deliver", deliver)
    connection = SimpleNamespace(host_id=host_id)
    app = SimpleNamespace()

    assert await startup._recover_session(app, receipt.child_id, connection) is False
    await startup._deliver_scoped(app, receipt.child_id, first)
    deliver.assert_not_awaited()
    startup.dispatch.assert_not_awaited()
    with pytest.raises(OmnigentError, match="activation is pending"):
        conversations.require_spawn_activated(receipt.child_id)

    if cancel_before_activation:
        await startup.cancel(receipt.child_id)
    assert spawns.advance_materialization(
        receipt,
        SpawnPhase.ACTIVATE,
        activation_check=SpawnActivationCheck(identity.runner_id, host_id, "/workspace", {}, None),
    )
    conversations.require_spawn_activated(receipt.child_id)
    try:
        busy = await startup._recover_session(app, receipt.child_id, connection)
        await asyncio.sleep(0)
        assert busy is not cancel_before_activation
        assert deliver.await_count == (0 if cancel_before_activation else 1)
        if cancel_before_activation:
            await startup._deliver_scoped(app, receipt.child_id, first)
            startup.dispatch.assert_not_awaited()
            assert conversations.get_item(receipt.child_id, first.id) is None
    finally:
        await startup.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["unavailable", "scope-revoked"])
async def test_coordinator_recovers_durable_first_input_after_activation_failure(
    spawn_context, failure
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock

    from omnigent.entities.conversation import MessageData, NewConversationItem
    from omnigent.server.host_registry import HostRegistry
    from omnigent.server.session_spawns import SessionSpawnCoordinator
    from omnigent.server.startup_inputs import (
        STARTUP_LABEL,
        ManagedStartupInputs,
        initial_input_id,
    )
    from omnigent.stores.conversation_store import PENDING_INPUT_RESPONSE_ID

    conversations, original_spawns, _, identity = spawn_context
    startup = ManagedStartupInputs(conversations, HostRegistry())

    async def activate(receipt, conversation):
        if failure == "unavailable":
            raise RuntimeError("activation unavailable")
        conversations.clear_runner_id(identity.parent_id)
        conversations.set_runner_id(identity.parent_id, "replacement-runner")
        return SpawnActivationCheck(identity.runner_id, None, None, {}, None)

    activation = AsyncMock(
        side_effect=activate,
        return_value=SpawnActivationCheck(identity.runner_id, None, None, {}, None),
    )
    coordinator = SessionSpawnCoordinator(
        conversations,
        startup,
        activate=activation,
        cleanup_blob=Mock(),
        artifact_store=original_spawns._artifacts,
    )
    spawns = coordinator.spawns
    receipt = _reserve(
        spawns,
        identity,
        payload={
            "arguments": {
                "title": "worker",
                "parent_conversation_id": identity.parent_id,
                "runner_id": identity.runner_id,
                "kind": "sub_agent",
                "sub_agent_name": "worker",
                "labels": {STARTUP_LABEL: "pending"},
            }
        },
    )
    first = NewConversationItem(
        stable_id=initial_input_id(receipt.child_id),
        type="message",
        status="in_progress",
        response_id=PENDING_INPUT_RESPONSE_ID,
        created_by=identity.owner_id,
        data=MessageData(role="user", content=[{"type": "input_text", "text": "first"}]),
    )
    receipt = spawns.admit(
        identity, receipt.nonce, first.model_dump(mode="json"), epoch=receipt.epoch
    )
    app = SimpleNamespace()
    await coordinator.advance(app, receipt)
    error = RuntimeError if failure == "unavailable" else SpawnAuthorityError
    with pytest.raises(error):
        await coordinator.advance(app, receipt)
    assert conversations.spawn_activation_pending(receipt.child_id)
    [stored] = conversations.list_pending_inputs(receipt.child_id)
    assert stored.id == first.stable_id and stored.status == "in_progress"

    async def dispatch(context, session_id, event):
        assert event.data["stable_id"] == first.stable_id
        assert await context.claim(conversations, session_id)
        return {"accepted": True}

    startup.dispatch = AsyncMock(side_effect=dispatch)
    if failure == "scope-revoked":
        conversations.clear_runner_id(identity.parent_id)
        conversations.set_runner_id(identity.parent_id, identity.runner_id)
    activation.side_effect = None
    await coordinator.advance(app, receipt)
    assert not conversations.spawn_activation_pending(receipt.child_id)
    try:
        await coordinator.advance(app, receipt)
        tasks = list(startup._deliveries.values())
        assert len(tasks) == 1
        await asyncio.gather(*tasks)
        await coordinator.advance(app, receipt)
        final = spawns.get(identity, receipt.nonce, epoch=receipt.epoch)
        assert final.phase == SpawnPhase.READY
        assert final.input_payload is None
        startup.dispatch.assert_awaited_once()
        await coordinator.advance(app, receipt)
        startup.dispatch.assert_awaited_once()
    finally:
        await startup.shutdown()
