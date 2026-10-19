"""Durable per-key versions and operation receipts in the preferences table.

A namespace row serializes writes across replicas. Receipts and tombstones do
not expire: bounded quotas reject new writes rather than silently losing retry
or deletion history. This API does not mark a workspace imported/adopted.
"""

from __future__ import annotations

from typing import TypeVar

from pydantic import BaseModel
from sqlalchemy import LargeBinary, delete, or_, select, type_coerce, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from omnigent.db.account_authority import require_active_account
from omnigent.db.compression import decode
from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.db.utils import (
    get_or_create_engine,
    make_named_managed_session_maker,
    run_write_transaction,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.preference_sync import (
    MAX_CONFLICTS,
    MAX_KEYS,
    MAX_OPERATIONS,
    MAX_RECORD_BYTES,
    Name,
    PreferenceMutation,
    PreferenceNamespace,
    PreferenceResult,
    PreferenceSnapshot,
    ProtocolModel,
    SharedPreferenceEntry,
)
from omnigent.server.auth import RESERVED_USER_LOCAL

_PREFIX = "shared.v1."
_META = _PREFIX + "meta"
_VALUE = _PREFIX + "value."
_RECEIPT = _PREFIX + "receipt."
_CONFLICT = _PREFIX + "conflict."
_Model = TypeVar("_Model", bound=BaseModel)


class _Metadata(ProtocolModel):
    authority_id: Name
    revision: int = 0
    keys: int = 0
    conflicts: int = 0


class _Receipt(ProtocolModel):
    operation: PreferenceMutation
    result: PreferenceResult


def _serialize(value: BaseModel) -> str:
    raw = value.model_dump_json()
    # Compression's raw framing adds two bytes; fit every supported BLOB backend.
    if len(raw.encode()) > MAX_RECORD_BYTES:
        raise OmnigentError(
            "Preference record exceeds storage limit", code=ErrorCode.INVALID_INPUT
        )
    return raw


def _decode(raw: bytes | str | memoryview, model: type[_Model]) -> _Model:
    return model.model_validate_json(decode(raw, max_decoded_bytes=MAX_RECORD_BYTES) or "null")


class SqlAlchemyPreferenceStore:
    def __init__(self, storage_location: str, *, authority_id: str) -> None:
        self._authority = _Metadata(authority_id=authority_id).authority_id
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine, query_name_prefix="omnigent.preference_store"
        )
        self._write_session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.preference_store",
            immediate=True,
        )

    def identity(self, user_id: str | None) -> PreferenceNamespace:
        return PreferenceNamespace(
            authority_id=self._authority,
            user_id=RESERVED_USER_LOCAL if user_id is None else user_id,
            workspace_id=str(current_workspace_id()),
        )

    def _scope(self, user_id: str | None):
        return (
            SqlPreference.workspace_id == current_workspace_id(),
            SqlPreference.user_id == self.identity(user_id).user_id,
        )

    def read(self, *, user_id: str | None) -> PreferenceSnapshot:
        snapshot = PreferenceSnapshot(identity=self.identity(user_id))
        with self._session("read_shared_preferences") as session:
            rows = session.execute(
                select(SqlPreference.key, type_coerce(SqlPreference.value, LargeBinary))
                .where(
                    *self._scope(user_id),
                    or_(
                        SqlPreference.key == _META,
                        SqlPreference.key.startswith(_VALUE),
                        SqlPreference.key.startswith(_CONFLICT),
                    ),
                )
                .order_by(SqlPreference.key)
            ).all()
        entries = []
        conflicts = []
        revision = 0
        for key, raw in rows:
            if key == _META:
                metadata = _decode(raw, _Metadata)
                self._check_authority(metadata)
                revision = metadata.revision
            elif key.startswith(_VALUE):
                entries.append(_decode(raw, SharedPreferenceEntry))
            else:
                conflicts.append(_decode(raw, PreferenceMutation))
        return snapshot.model_copy(
            update={"revision": revision, "entries": entries, "conflicts": conflicts}
        )

    def _check_authority(self, metadata: _Metadata) -> None:
        if metadata.authority_id != self._authority:
            raise OmnigentError(
                "Preference authority does not match stored state", code=ErrorCode.CONFLICT
            )

    def apply(self, operation: PreferenceMutation, *, user_id: str | None) -> PreferenceResult:
        if operation.identity != self.identity(user_id):
            raise OmnigentError(
                "Preference identity does not match authenticated context",
                code=ErrorCode.FORBIDDEN,
            )

        def write(session: Session) -> PreferenceResult:
            require_active_account(session, user_id)
            scope = self._scope(user_id)
            values = {
                "workspace_id": current_workspace_id(),
                "user_id": self.identity(user_id).user_id,
                "key": _META,
                "value": _serialize(_Metadata(authority_id=self._authority)),
            }
            dialect = self._engine.dialect.name
            if dialect == "mysql":
                insert = (
                    mysql_insert(SqlPreference).values(**values).on_duplicate_key_update(key=_META)
                )
            elif dialect == "sqlite":
                insert = sqlite_insert(SqlPreference).values(**values).on_conflict_do_nothing()
            else:
                insert = pg_insert(SqlPreference).values(**values).on_conflict_do_nothing()
            session.execute(insert)
            raw = session.scalar(
                select(type_coerce(SqlPreference.value, LargeBinary))
                .where(*scope, SqlPreference.key == _META)
                .with_for_update()
            )
            assert raw is not None
            metadata = _decode(raw, _Metadata)
            self._check_authority(metadata)

            def read_record(key: str, model: type[_Model]) -> _Model | None:
                raw = session.scalar(
                    select(type_coerce(SqlPreference.value, LargeBinary)).where(
                        *scope, SqlPreference.key == key
                    )
                )
                return None if raw is None else _decode(raw, model)

            def add_record(key: str, value: BaseModel) -> None:
                session.add(
                    SqlPreference(
                        workspace_id=current_workspace_id(),
                        user_id=self.identity(user_id).user_id,
                        key=key,
                        value=_serialize(value),
                    )
                )

            receipt_key = _RECEIPT + operation.operation_id.hex
            if receipt := read_record(receipt_key, _Receipt):
                if receipt.operation != operation:
                    raise OmnigentError(
                        "Operation ID already used for a different edit", code=ErrorCode.CONFLICT
                    )
                return receipt.result
            if metadata.revision >= MAX_OPERATIONS:
                raise OmnigentError(
                    "Preference operation history is full; migration required",
                    code=ErrorCode.CONFLICT,
                )
            value_key = _VALUE + operation.key.storage_key()
            previous = read_record(value_key, SharedPreferenceEntry)
            current = previous or SharedPreferenceEntry(key=operation.key, version=0, value=None)
            if current.key != operation.key:
                raise OmnigentError("Preference key collision", code=ErrorCode.CONFLICT)
            if operation.expected_version > current.version:
                raise OmnigentError(
                    "Preference version is ahead of the server", code=ErrorCode.CONFLICT
                )
            for conflict_id in operation.resolves:
                conflict = read_record(_CONFLICT + conflict_id.hex, PreferenceMutation)
                if conflict is None or conflict.key != operation.key:
                    raise OmnigentError(
                        "Conflict no longer exists for this key", code=ErrorCode.CONFLICT
                    )
            stale = operation.expected_version != current.version
            if stale and (operation.value != current.value or operation.resolves):
                if metadata.conflicts >= MAX_CONFLICTS:
                    raise OmnigentError(
                        "Resolve outstanding preference conflicts first", code=ErrorCode.CONFLICT
                    )
                add_record(_CONFLICT + operation.operation_id.hex, operation)
                metadata = metadata.model_copy(update={"conflicts": metadata.conflicts + 1})
                status = "conflict"
            else:
                status = "unchanged"
                if previous is None or operation.value != current.value:
                    if previous is None and metadata.keys >= MAX_KEYS:
                        raise OmnigentError(
                            "Preference key limit reached", code=ErrorCode.CONFLICT
                        )
                    current = SharedPreferenceEntry(
                        key=operation.key,
                        version=current.version + 1,
                        value=operation.value,
                        installation_id=operation.installation_id,
                    )
                    if previous is None:
                        add_record(value_key, current)
                        metadata = metadata.model_copy(update={"keys": metadata.keys + 1})
                    else:
                        session.execute(
                            update(SqlPreference)
                            .where(*scope, SqlPreference.key == value_key)
                            .values(value=_serialize(current))
                        )
                    status = "applied"
                if operation.resolves:
                    session.execute(
                        delete(SqlPreference).where(
                            *scope,
                            SqlPreference.key.in_(
                                [_CONFLICT + item.hex for item in operation.resolves]
                            ),
                        )
                    )
                    metadata = metadata.model_copy(
                        update={"conflicts": metadata.conflicts - len(operation.resolves)}
                    )
            metadata = metadata.model_copy(update={"revision": metadata.revision + 1})
            result = PreferenceResult(
                operation_id=operation.operation_id,
                status=status,
                revision=metadata.revision,
                entry=current,
            )
            add_record(receipt_key, _Receipt(operation=operation, result=result))
            session.execute(
                update(SqlPreference)
                .where(*scope, SqlPreference.key == _META)
                .values(value=_serialize(metadata))
            )
            return result

        return run_write_transaction(self._write_session, "apply_shared_preference", write)
