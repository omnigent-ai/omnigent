from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import desc, select, update
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import (
    DEFAULT_WORKSPACE_ID,
    SqlDpiaCase,
    SqlDpiaCaseRevision,
    current_workspace_id,
)
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker, now_epoch
from omnigent.entities import DpiaCaseRecord, DpiaCaseRevision
from omnigent.stores.dpia_case_store.base import DpiaCaseConflictError, DpiaCaseStore

_CASE_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
MAX_DPIA_SNAPSHOT_BYTES = 2 * 1024 * 1024


def _snapshot_json(case_id: str, snapshot: dict[str, Any]) -> str:
    if _CASE_ID.fullmatch(case_id) is None:
        raise ValueError("DPIA case id must be a lowercase slug")
    if snapshot.get("id") != case_id:
        raise ValueError("DPIA snapshot id must match its case id")
    processing_model = snapshot.get("processingModel")
    if not isinstance(processing_model, dict) or processing_model.get("caseId") != case_id:
        raise ValueError("DPIA processing model must match its case id")
    version = processing_model.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version < 1:
        raise ValueError("DPIA processing model version must be a positive integer")
    audit = snapshot.get("audit")
    if not isinstance(audit, list):
        raise ValueError("DPIA snapshot audit must be a list")
    serialized = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if len(serialized.encode("utf-8")) > MAX_DPIA_SNAPSHOT_BYTES:
        raise ValueError("DPIA snapshot exceeds the 2 MiB limit")
    return serialized


def _record(row: SqlDpiaCase) -> DpiaCaseRecord:
    return DpiaCaseRecord(
        workspace_id=row.workspace_id or DEFAULT_WORKSPACE_ID,
        case_id=row.case_id,
        revision=row.revision,
        snapshot=json.loads(row.snapshot_json),
        created_by=row.created_by,
        updated_by=row.updated_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _revision(row: SqlDpiaCaseRevision) -> DpiaCaseRevision:
    return DpiaCaseRevision(
        workspace_id=row.workspace_id or DEFAULT_WORKSPACE_ID,
        case_id=row.case_id,
        revision=row.revision,
        snapshot=json.loads(row.snapshot_json),
        actor=row.actor,
        created_at=row.created_at,
    )


class SqlAlchemyDpiaCaseStore(DpiaCaseStore):
    def __init__(self, storage_location: str) -> None:
        super().__init__(storage_location)
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.dpia_case_store",
        )

    def get_case(self, case_id: str) -> DpiaCaseRecord | None:
        with self._session("get_case") as session:
            row = session.get(SqlDpiaCase, (current_workspace_id(), case_id))
            return _record(row) if row else None

    def list_cases(self) -> list[DpiaCaseRecord]:
        with self._session("list_cases") as session:
            rows = session.execute(
                select(SqlDpiaCase)
                .where(SqlDpiaCase.workspace_id == current_workspace_id())
                .order_by(desc(SqlDpiaCase.updated_at), SqlDpiaCase.case_id)
            ).scalars()
            return [_record(row) for row in rows]

    def list_revisions(self, case_id: str, *, limit: int = 100) -> list[DpiaCaseRevision]:
        if limit < 1 or limit > 500:
            raise ValueError("DPIA revision limit must be between 1 and 500")
        with self._session("list_revisions") as session:
            rows = list(
                session.execute(
                    select(SqlDpiaCaseRevision)
                    .where(
                        SqlDpiaCaseRevision.workspace_id == current_workspace_id(),
                        SqlDpiaCaseRevision.case_id == case_id,
                    )
                    .order_by(desc(SqlDpiaCaseRevision.revision))
                    .limit(limit)
                ).scalars()
            )
            return [_revision(row) for row in reversed(rows)]

    def get_revision(self, case_id: str, revision: int) -> DpiaCaseRevision | None:
        with self._session("get_revision") as session:
            row = session.get(
                SqlDpiaCaseRevision,
                (current_workspace_id(), case_id, revision),
            )
            return _revision(row) if row else None

    def save_case(
        self,
        case_id: str,
        snapshot: dict[str, Any],
        *,
        expected_revision: int,
        actor: str,
    ) -> DpiaCaseRecord:
        if expected_revision < 0:
            raise ValueError("expected_revision must not be negative")
        normalized_actor = actor.strip()
        if not normalized_actor or len(normalized_actor) > 128:
            raise ValueError("DPIA case actor must be between 1 and 128 characters")
        serialized = _snapshot_json(case_id, snapshot)
        workspace_id = current_workspace_id()
        saved_at = now_epoch()
        next_revision = expected_revision + 1
        try:
            with self._session("save_case") as session:
                if expected_revision == 0:
                    row = SqlDpiaCase(
                        case_id=case_id,
                        revision=1,
                        snapshot_json=serialized,
                        created_by=normalized_actor,
                        updated_by=normalized_actor,
                        created_at=saved_at,
                        updated_at=saved_at,
                    )
                    session.add(row)
                else:
                    result = session.execute(
                        update(SqlDpiaCase)
                        .where(
                            SqlDpiaCase.workspace_id == workspace_id,
                            SqlDpiaCase.case_id == case_id,
                            SqlDpiaCase.revision == expected_revision,
                        )
                        .values(
                            revision=next_revision,
                            snapshot_json=serialized,
                            updated_by=normalized_actor,
                            updated_at=saved_at,
                        )
                    )
                    if getattr(result, "rowcount", 0) != 1:
                        current = session.get(SqlDpiaCase, (workspace_id, case_id))
                        raise DpiaCaseConflictError(current.revision if current else 0)
                    row = session.get(SqlDpiaCase, (workspace_id, case_id))
                    if row is None:
                        raise RuntimeError("DPIA case update was lost")
                session.add(
                    SqlDpiaCaseRevision(
                        case_id=case_id,
                        revision=next_revision,
                        snapshot_json=serialized,
                        actor=normalized_actor,
                        created_at=saved_at,
                    )
                )
                session.flush()
                return _record(row)
        except IntegrityError as exc:
            current = self.get_case(case_id)
            raise DpiaCaseConflictError(current.revision if current else 0) from exc
