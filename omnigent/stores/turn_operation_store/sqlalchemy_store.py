"""SQLAlchemy-backed durable turn-operation journal."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import DEFAULT_WORKSPACE_ID, SqlTurnOperation, current_workspace_id
from omnigent.db.enum_codecs import decode_turn_operation_state, encode_turn_operation_state
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker, now_epoch
from omnigent.entities import TURN_OPERATION_TERMINAL_STATES, TurnOperation
from omnigent.stores.turn_operation_store import (
    TurnOperationConflict,
    TurnOperationStateError,
    TurnOperationStore,
)

_DISPATCH_STATES = frozenset({"dispatched", "dispatch_unknown"})


def _digest(domain: str, value: str) -> bytes:
    payload = f"omnigent.turn-operation/v1/{domain}\0{value}".encode()
    return hashlib.sha256(payload).digest()


def _digest_hex(value: bytes) -> str:
    return bytes(value).hex()


def _canonical_request(request: dict[str, Any]) -> str:
    try:
        encoded = json.dumps(
            request,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("turn request must be canonical JSON data") from exc
    if len(encoded.encode()) > 8 * 1024 * 1024:
        raise ValueError("turn request exceeds the 8 MiB journal limit")
    return encoded


def _validate_key(key: str) -> None:
    if not 1 <= len(key) <= 255 or any(ord(ch) < 0x21 or ord(ch) > 0x7E for ch in key):
        raise ValueError("idempotency key must be 1-255 visible ASCII characters")


def _validate_principal(principal: str) -> None:
    if not principal or len(principal.encode()) > 512:
        raise ValueError("principal must be non-empty and at most 512 UTF-8 bytes")


def _to_entity(row: SqlTurnOperation) -> TurnOperation:
    return TurnOperation(
        id=row.id,
        conversation_id=row.conversation_id,
        principal_hash=_digest_hex(row.principal_hash),
        idempotency_key_hash=_digest_hex(row.idempotency_key_hash),
        request_hash=_digest_hex(row.request_hash),
        request_json=row.request_json,
        state=decode_turn_operation_state(row.state),
        created_at=row.created_at,
        workspace_id=row.workspace_id or DEFAULT_WORKSPACE_ID,
        item_id=row.item_id,
        updated_at=row.updated_at,
        terminal_at=row.terminal_at,
        error_code=row.error_code,
        error=row.error,
    )


class SqlAlchemyTurnOperationStore(TurnOperationStore):
    """Database-enforced replay journal for externally submitted turns."""

    def __init__(self, storage_location: str) -> None:
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.turn_operation_store",
        )
        self._write_session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.turn_operation_store",
            immediate=True,
        )

    def _find_by_replay_key(
        self,
        conversation_id: str,
        principal_hash: bytes,
        key_hash: bytes,
    ) -> SqlTurnOperation | None:
        with self._session("select_by_replay_key") as session:
            return session.execute(
                select(SqlTurnOperation).where(
                    SqlTurnOperation.workspace_id == current_workspace_id(),
                    SqlTurnOperation.conversation_id == conversation_id,
                    SqlTurnOperation.principal_hash == principal_hash,
                    SqlTurnOperation.idempotency_key_hash == key_hash,
                )
            ).scalar_one_or_none()

    def create_or_get(
        self,
        *,
        conversation_id: str,
        principal: str,
        idempotency_key: str,
        request: dict[str, Any],
    ) -> tuple[TurnOperation, bool]:
        _validate_principal(principal)
        _validate_key(idempotency_key)
        request_json = _canonical_request(request)
        principal_hash = _digest("principal", principal)
        key_hash = _digest("idempotency-key", idempotency_key)
        request_hash = _digest("request", request_json)

        existing = self._find_by_replay_key(conversation_id, principal_hash, key_hash)
        if existing is not None:
            return self._validate_replay(existing, request_hash, request_json), False

        row = SqlTurnOperation(
            id=uuid.uuid4().hex,
            conversation_id=conversation_id,
            principal_hash=principal_hash,
            idempotency_key_hash=key_hash,
            request_hash=request_hash,
            request_json=request_json,
            state=encode_turn_operation_state("accepted"),
            item_id=None,
            created_at=now_epoch(),
            updated_at=None,
            terminal_at=None,
            error_code=None,
            error=None,
        )
        try:
            with self._write_session("insert_operation") as session:
                session.add(row)
                session.flush()
                created = _to_entity(row)
        except IntegrityError:
            # A concurrent insert won the database unique key. Read and validate
            # that row instead of inventing an application-level lock.
            winner = self._find_by_replay_key(conversation_id, principal_hash, key_hash)
            if winner is None:
                raise
            return self._validate_replay(winner, request_hash, request_json), False
        return created, True

    @staticmethod
    def _validate_replay(
        row: SqlTurnOperation,
        request_hash: bytes,
        request_json: str,
    ) -> TurnOperation:
        if bytes(row.request_hash) != request_hash or row.request_json != request_json:
            raise TurnOperationConflict("idempotency key was already used for another request")
        return _to_entity(row)

    def get(self, operation_id: str) -> TurnOperation | None:
        with self._session("select_by_id") as session:
            row = session.get(SqlTurnOperation, (current_workspace_id(), operation_id))
            return None if row is None else _to_entity(row)

    def _require_row(self, session: Any, operation_id: str) -> SqlTurnOperation:
        # PostgreSQL/MySQL need a row lock for read-check-write lifecycle
        # transitions. SQLite ignores FOR UPDATE but the write session already
        # holds BEGIN IMMEDIATE, so all supported databases serialize here.
        row = session.get(
            SqlTurnOperation,
            (current_workspace_id(), operation_id),
            with_for_update=True,
        )
        if row is None:
            raise KeyError(operation_id)
        return row

    def mark_input_persisted(self, operation_id: str, item_id: str) -> TurnOperation:
        with self._write_session("mark_input_persisted") as session:
            row = self._require_row(session, operation_id)
            state = decode_turn_operation_state(row.state)
            if state == "accepted":
                row.state = encode_turn_operation_state("input_persisted")
                row.item_id = item_id
                row.updated_at = now_epoch()
            elif row.item_id != item_id:
                raise TurnOperationStateError("operation is already bound to another input item")
            return _to_entity(row)

    def mark_dispatch_result(
        self,
        operation_id: str,
        *,
        state: str,
        error_code: str | None = None,
        error: str | None = None,
    ) -> TurnOperation:
        if state not in _DISPATCH_STATES:
            raise ValueError("dispatch result must be dispatched or dispatch_unknown")
        with self._write_session("mark_dispatch_result") as session:
            row = self._require_row(session, operation_id)
            current = decode_turn_operation_state(row.state)
            if current == "input_persisted" or (
                current == "dispatch_unknown" and state == "dispatched"
            ):
                row.state = encode_turn_operation_state(state)
                row.updated_at = now_epoch()
                row.error_code = error_code
                row.error = error
            elif current != state:
                raise TurnOperationStateError(f"cannot transition {current} to {state}")
            elif row.error_code != error_code or row.error != error:
                raise TurnOperationStateError("dispatch replay changed the recorded outcome")
            return _to_entity(row)

    def mark_terminal(
        self,
        operation_id: str,
        *,
        state: str,
        error_code: str | None = None,
        error: str | None = None,
    ) -> TurnOperation:
        if state not in TURN_OPERATION_TERMINAL_STATES:
            raise ValueError("terminal state is invalid")
        with self._write_session("mark_terminal") as session:
            row = self._require_row(session, operation_id)
            current = decode_turn_operation_state(row.state)
            if current in _DISPATCH_STATES:
                now = now_epoch()
                row.state = encode_turn_operation_state(state)
                row.updated_at = now
                row.terminal_at = now
                row.error_code = error_code
                row.error = error
            elif current != state:
                raise TurnOperationStateError(f"cannot transition {current} to {state}")
            elif row.error_code != error_code or row.error != error:
                raise TurnOperationStateError("terminal replay changed the recorded outcome")
            return _to_entity(row)

    def delete_for_conversation(self, conversation_id: str) -> int:
        with self._write_session("delete_for_conversation") as session:
            result = session.execute(
                delete(SqlTurnOperation).where(
                    SqlTurnOperation.workspace_id == current_workspace_id(),
                    SqlTurnOperation.conversation_id == conversation_id,
                )
            )
            return int(cast(CursorResult[Any], result).rowcount or 0)
