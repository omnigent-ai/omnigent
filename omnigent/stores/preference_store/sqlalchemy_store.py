"""Per-key versions, conflicts and retry receipts in preferences rows.

A namespace row serializes writers.
"""

from __future__ import annotations

import base64
import hashlib
import json
from typing import Annotated, TypeVar

from pydantic import BaseModel, Field
from sqlalchemy import LargeBinary, delete, select, type_coerce, union_all, update
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
    PREFERENCE_PAGE_SIZE,
    Name,
    PreferenceMutation,
    PreferenceNamespace,
    PreferencePage,
    PreferenceResult,
    ProtocolModel,
    SharedPreferenceEntry,
    Version,
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


class _Cursor(ProtocolModel):
    identity: PreferenceNamespace
    revision: Version
    after: Annotated[
        str, Field(pattern=r"^shared\.v1\.(?:conflict\.[0-9a-f]{32}|value\.[0-9a-f]{64})$")
    ]


class _Receipt(ProtocolModel):
    fingerprint: str
    result: PreferenceResult


def _fingerprint(operation: PreferenceMutation) -> str:
    payload = {
        "protocol_version": operation.protocol_version,
        "authority_id": operation.identity.authority_id,
        "user_id": operation.identity.user_id,
        "workspace_id": operation.identity.workspace_id,
        "operation_id": str(operation.operation_id),
        "installation_id": operation.installation_id,
        "key": operation.key.storage_key(),
        "expected_version": operation.expected_version,
        "value": operation.value,
        "resolves": [str(conflict) for conflict in operation.resolves],
    }
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _serialize(value: BaseModel) -> str:
    raw = value.model_dump_json()
    # Bound the raw record independently of compression, with room for framing.
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

    def read(
        self, *, user_id: str | None, limit: int = PREFERENCE_PAGE_SIZE, cursor: str | None = None
    ) -> PreferencePage:
        if type(limit) is not int or not 1 <= limit <= PREFERENCE_PAGE_SIZE:
            raise OmnigentError("Invalid preference page size", code=ErrorCode.INVALID_INPUT)
        identity = self.identity(user_id)
        continuation = None
        if cursor is not None:
            try:
                if len(cursor) > 4096:
                    raise ValueError("Cursor too long")
                continuation = _Cursor.model_validate_json(
                    base64.b64decode(cursor, altchars=b"-_", validate=True)
                )
            except ValueError as exc:
                raise OmnigentError(
                    "Invalid preference cursor", code=ErrorCode.INVALID_INPUT
                ) from exc
            if continuation.identity != identity:
                raise OmnigentError(
                    "Preference cursor identity mismatch", code=ErrorCode.INVALID_INPUT
                )
        scope = self._scope(user_id)
        columns = (SqlPreference.key, type_coerce(SqlPreference.value, LargeBinary).label("value"))
        queries = [select(*columns).where(*scope, SqlPreference.key == _META)]
        for prefix in (_CONFLICT, _VALUE):
            if (
                continuation is not None
                and continuation.after.startswith(_VALUE)
                and prefix == _CONFLICT
            ):
                continue
            query = select(*columns).where(
                *scope, SqlPreference.key >= prefix + "0", SqlPreference.key < prefix + "g"
            )
            if continuation is not None and continuation.after.startswith(prefix):
                query = query.where(SqlPreference.key > continuation.after)
            bounded = query.order_by(SqlPreference.key).limit(limit + 1).subquery()
            queries.append(select(bounded.c.key, bounded.c.value))
        # One statement gives each page a consistent view; every range is independently bounded.
        with self._session("read_shared_preferences") as session:
            records = session.execute(union_all(*queries)).all()
        metadata = next(
            (_decode(raw, _Metadata) for key, raw in records if key == _META),
            _Metadata(authority_id=self._authority),
        )
        self._check_authority(metadata)
        if continuation is not None and continuation.revision != metadata.revision:
            raise OmnigentError(
                "Preference revision changed; restart pagination", code=ErrorCode.CONFLICT
            )
        rows = sorted((key, raw) for key, raw in records if key != _META)
        next_cursor = None
        if len(rows) > limit:
            rows = rows[:limit]
            next_cursor = base64.urlsafe_b64encode(
                _Cursor(identity=identity, revision=metadata.revision, after=rows[-1][0])
                .model_dump_json()
                .encode()
            ).decode()
        return PreferencePage(
            identity=identity,
            revision=metadata.revision,
            next_cursor=next_cursor,
            entries=[
                _decode(value, SharedPreferenceEntry)
                for key, value in rows
                if key.startswith(_VALUE)
            ],
            conflicts=[
                _decode(value, PreferenceMutation)
                for key, value in rows
                if key.startswith(_CONFLICT)
            ],
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
                # Current reads avoid stale snapshots under MySQL REPEATABLE READ.
                raw = session.scalar(
                    select(type_coerce(SqlPreference.value, LargeBinary))
                    .where(*scope, SqlPreference.key == key)
                    .with_for_update()
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
                if receipt.fingerprint != _fingerprint(operation):
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
            add_record(receipt_key, _Receipt(fingerprint=_fingerprint(operation), result=result))
            session.execute(
                update(SqlPreference)
                .where(*scope, SqlPreference.key == _META)
                .values(value=_serialize(metadata))
            )
            return result

        return run_write_transaction(self._write_session, "apply_shared_preference", write)
