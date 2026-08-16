"""Tests for the durable turn-operation replay journal."""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.stores.turn_operation_store import (
    TurnOperationConflict,
    TurnOperationStateError,
)
from omnigent.stores.turn_operation_store.sqlalchemy_store import (
    SqlAlchemyTurnOperationStore,
)


def _uid(seed: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyTurnOperationStore:
    return SqlAlchemyTurnOperationStore(db_uri)


def _create(
    store: SqlAlchemyTurnOperationStore,
    *,
    conversation_id: str | None = None,
    key: str = "attempt-1",
    request: dict[str, object] | None = None,
):
    return store.create_or_get(
        conversation_id=conversation_id or _uid("conversation"),
        principal="agentfactory:workflow-principal",
        idempotency_key=key,
        request=request or {"message": {"content": "build it"}, "version": "v1alpha1"},
    )


def _record_attempt(
    store: SqlAlchemyTurnOperationStore,
    operation_id: str,
    incarnation: str | None = None,
):
    return store.record_dispatch_attempt(operation_id, incarnation or _uid("runner"))


def _mark_input(
    store: SqlAlchemyTurnOperationStore,
    operation_id: str,
    item_id: str | None = None,
    dispatch_request: dict[str, object] | None = None,
):
    return store.mark_input_persisted(
        operation_id,
        item_id or _uid("input-item"),
        dispatch_request or {"type": "message", "content": "build it"},
    )


def test_create_and_exact_replay_return_one_operation(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    first, created = _create(store)
    replay, replay_created = _create(
        store,
        request={"version": "v1alpha1", "message": {"content": "build it"}},
    )

    assert created is True
    assert replay_created is False
    assert replay == first
    assert first.state == "accepted"
    assert first.item_id is None
    assert json.loads(first.request_json) == {
        "message": {"content": "build it"},
        "version": "v1alpha1",
    }
    assert len(first.principal_hash) == 64
    assert len(first.idempotency_key_hash) == 64
    assert len(first.request_hash) == 64
    assert "attempt-1" not in first.request_json
    assert "workflow-principal" not in first.request_json


def test_prepare_freezes_input_without_claiming_it_is_persisted(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    operation, _ = _create(store)
    item_id = _uid("prepared-item")
    dispatch = {"type": "message", "content": "build it"}

    prepared = store.prepare_input(operation.id, item_id, dispatch)
    replay = store.prepare_input(operation.id, item_id, dispatch)

    assert prepared.state == "accepted"
    assert prepared.item_id == item_id
    assert prepared.dispatch_request_json is not None
    assert replay == prepared

    persisted = store.mark_input_persisted(operation.id, item_id, dispatch)
    assert persisted.state == "input_persisted"
    assert persisted.item_id == item_id


def test_prepare_rejects_item_or_dispatch_drift(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    operation, _ = _create(store)
    item_id = _uid("prepared-item")
    store.prepare_input(operation.id, item_id, {"type": "message", "content": "build it"})

    with pytest.raises(TurnOperationStateError, match="already prepared"):
        store.prepare_input(
            operation.id,
            _uid("other-item"),
            {"type": "message", "content": "build it"},
        )
    with pytest.raises(TurnOperationStateError, match="already prepared"):
        store.prepare_input(
            operation.id,
            item_id,
            {"type": "message", "content": "changed"},
        )


def test_same_key_changed_body_conflicts(store: SqlAlchemyTurnOperationStore) -> None:
    _create(store)
    with pytest.raises(TurnOperationConflict, match="another request"):
        _create(store, request={"message": {"content": "different"}})


@pytest.mark.parametrize(
    "key",
    ["", "contains space", "line\nbreak", "x" * 256, "snowman-☃"],
)
def test_invalid_idempotency_keys_are_rejected(
    store: SqlAlchemyTurnOperationStore,
    key: str,
) -> None:
    with pytest.raises(ValueError, match="visible ASCII"):
        _create(store, key=key)


@pytest.mark.parametrize("principal", ["", "☃" * 257])
def test_invalid_principal_is_rejected(
    store: SqlAlchemyTurnOperationStore,
    principal: str,
) -> None:
    with pytest.raises(ValueError, match="principal"):
        store.create_or_get(
            conversation_id=_uid("conversation"),
            principal=principal,
            idempotency_key="attempt-1",
            request={"message": {"content": "build it"}},
        )


def test_request_must_be_finite_json_and_bounded(store: SqlAlchemyTurnOperationStore) -> None:
    with pytest.raises(ValueError, match="canonical JSON"):
        _create(store, request={"value": float("nan")})
    with pytest.raises(ValueError, match="8 MiB"):
        _create(store, key="large", request={"value": "x" * (8 * 1024 * 1024)})


def test_lifecycle_and_exact_replays_are_idempotent(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    operation, _ = _create(store)
    item_id = _uid("input-item")

    persisted = _mark_input(store, operation.id, item_id)
    assert persisted.state == "input_persisted"
    assert _mark_input(store, operation.id, item_id) == persisted
    assert json.loads(persisted.dispatch_request_json or "") == {
        "content": "build it",
        "type": "message",
    }
    assert persisted.dispatch_request_hash is not None

    attempted = _record_attempt(store, operation.id)
    assert attempted.runner_incarnation_id == _uid("runner")
    assert attempted.dispatch_attempts == 1
    assert attempted.last_dispatch_at is not None

    dispatched = store.mark_dispatch_result(operation.id, state="dispatched")
    assert dispatched.state == "dispatched"
    assert store.mark_dispatch_result(operation.id, state="dispatched") == dispatched

    terminal = store.mark_terminal(operation.id, state="succeeded")
    assert terminal.state == "succeeded"
    assert terminal.terminal_at is not None
    assert store.mark_terminal(operation.id, state="succeeded") == terminal
    assert store.get(operation.id) == terminal


def test_transport_ambiguity_requires_explicit_reconciliation(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    operation, _ = _create(store)
    _mark_input(store, operation.id)
    _record_attempt(store, operation.id)
    unknown = store.mark_dispatch_result(
        operation.id,
        state="dispatch_unknown",
        error_code="runner_timeout",
        error="runner acceptance was not observed",
    )
    assert unknown.state == "dispatch_unknown"

    with pytest.raises(TurnOperationStateError, match="recorded outcome"):
        store.mark_dispatch_result(operation.id, state="dispatch_unknown")

    reconciled = store.mark_dispatch_result(operation.id, state="dispatched")
    assert reconciled.state == "dispatched"
    assert reconciled.error is None


def test_dispatch_attempt_binding_is_strict_and_retry_audited(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    operation, _ = _create(store)
    _mark_input(store, operation.id)
    with pytest.raises(ValueError, match="32 lowercase"):
        store.record_dispatch_attempt(operation.id, "BAD")

    first = _record_attempt(store, operation.id, _uid("runner-a"))
    assert first.dispatch_attempts == 1
    store.mark_dispatch_result(
        operation.id,
        state="dispatch_unknown",
        error_code="runner_timeout",
        error="first attempt",
    )
    retry = _record_attempt(store, operation.id, _uid("runner-a"))
    assert retry.dispatch_attempts == 2
    assert retry.error_code is None
    assert retry.error is None

    with pytest.raises(TurnOperationStateError, match="another runner incarnation"):
        _record_attempt(store, operation.id, _uid("runner-b"))
    store.mark_dispatch_result(operation.id, state="dispatched")
    with pytest.raises(TurnOperationStateError, match="while it is dispatched"):
        _record_attempt(store, operation.id, _uid("runner-a"))


def test_terminal_report_can_resolve_dispatch_unknown(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    operation, _ = _create(store)
    _mark_input(store, operation.id)
    _record_attempt(store, operation.id)
    store.mark_dispatch_result(operation.id, state="dispatch_unknown")
    terminal = store.mark_terminal(
        operation.id,
        state="failed",
        error_code="provider_failed",
        error="provider returned a terminal failure",
    )
    assert terminal.state == "failed"
    assert terminal.error_code == "provider_failed"


def test_invalid_or_conflicting_transitions_fail_closed(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    operation, _ = _create(store)
    with pytest.raises(TurnOperationStateError, match="accepted to dispatched"):
        store.mark_dispatch_result(operation.id, state="dispatched")
    with pytest.raises(TurnOperationStateError, match="accepted to succeeded"):
        store.mark_terminal(operation.id, state="succeeded")

    item_id = _uid("input-item")
    _mark_input(store, operation.id, item_id)
    with pytest.raises(TurnOperationStateError, match="another input item or dispatch request"):
        _mark_input(store, operation.id, _uid("other-item"))
    with pytest.raises(TurnOperationStateError, match="another input item or dispatch request"):
        _mark_input(store, operation.id, item_id, {"content": "changed"})
    with pytest.raises(ValueError, match="dispatch result"):
        store.mark_dispatch_result(operation.id, state="failed")
    with pytest.raises(ValueError, match="terminal state"):
        store.mark_terminal(operation.id, state="running")

    with pytest.raises(TurnOperationStateError, match="recorded runner incarnation"):
        store.mark_dispatch_result(operation.id, state="dispatched")
    _record_attempt(store, operation.id)
    store.mark_dispatch_result(operation.id, state="dispatched")
    store.mark_terminal(operation.id, state="failed", error="first report")
    with pytest.raises(TurnOperationStateError, match="terminal replay changed"):
        store.mark_terminal(operation.id, state="failed", error="changed report")


def test_unknown_operation_reads_none_and_mutations_raise_key_error(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    operation_id = _uid("unknown-operation")
    assert store.get(operation_id) is None
    with pytest.raises(KeyError, match=operation_id):
        _mark_input(store, operation_id)


def test_workspace_and_conversation_scope_are_independent(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    conversation_id = _uid("conversation")
    with workspace_scope(1):
        first, _ = _create(store, conversation_id=conversation_id)
    with workspace_scope(2):
        second, _ = _create(store, conversation_id=conversation_id)
    assert first.id != second.id
    assert first.workspace_id == 1
    assert second.workspace_id == 2


def test_concurrent_create_converges_on_database_unique_key(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path}/turn-operations.db"
    store = SqlAlchemyTurnOperationStore(uri)

    def create() -> tuple[str, bool]:
        operation, created = _create(store)
        return operation.id, created

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: create(), range(24)))

    assert len({operation_id for operation_id, _ in results}) == 1
    assert sum(1 for _, created in results if created) == 1


def test_concurrent_conflicting_terminal_reports_have_one_winner(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path}/turn-terminal.db"
    store = SqlAlchemyTurnOperationStore(uri)
    operation, _ = _create(store)
    _mark_input(store, operation.id)
    _record_attempt(store, operation.id)
    store.mark_dispatch_result(operation.id, state="dispatched")

    def report(state: str) -> str:
        try:
            return store.mark_terminal(operation.id, state=state).state
        except TurnOperationStateError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(report, ["succeeded", "failed"]))

    assert results.count("conflict") == 1
    winner = next(state for state in results if state != "conflict")
    durable = store.get(operation.id)
    assert durable is not None
    assert durable.state == winner


def test_delete_for_conversation_is_scoped_and_idempotent(
    store: SqlAlchemyTurnOperationStore,
) -> None:
    conversation = _uid("conversation")
    other = _uid("other-conversation")
    _create(store, conversation_id=conversation, key="one")
    _create(store, conversation_id=conversation, key="two")
    survivor, _ = _create(store, conversation_id=other)

    assert store.delete_for_conversation(conversation) == 2
    assert store.delete_for_conversation(conversation) == 0
    assert store.get(survivor.id) == survivor
