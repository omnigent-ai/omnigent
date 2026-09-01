from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, cast

from sqlalchemy import asc, select, update
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import (
    SqlMemoryErasureAttempt,
    SqlMemoryErasureRequest,
    SqlMemoryErasureTask,
)
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker
from omnigent.memory.erasure_models import (
    MemoryErasure,
    MemoryErasureAttempt,
    MemoryErasureRequestStatus,
    MemoryErasureTask,
    MemoryErasureTaskStatus,
)
from omnigent.memory.models import MemoryScope
from omnigent.spec.types import MemoryProviderName, MemoryScopeKind


class MemoryErasureStateError(RuntimeError):
    pass


class MemoryErasureLeaseLostError(MemoryErasureStateError):
    pass


def _stable_id(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:32]


def _erasure(row: SqlMemoryErasureRequest) -> MemoryErasure:
    return MemoryErasure(
        id=row.id,
        workspace_id=row.workspace_id,
        operation_id=row.operation_id,
        requested_by=row.requested_by,
        scope_kind=cast(MemoryScopeKind, row.scope_kind),
        scope_subject=row.scope_subject,
        status=cast(MemoryErasureRequestStatus, row.status),
        requested_at=row.requested_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
        last_error=row.last_error,
    )


def _task(row: SqlMemoryErasureTask) -> MemoryErasureTask:
    return MemoryErasureTask(
        id=row.id,
        workspace_id=row.workspace_id,
        erasure_id=row.erasure_id,
        provider=cast(MemoryProviderName, row.provider),
        operation_id=row.operation_id,
        status=cast(MemoryErasureTaskStatus, row.status),
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        next_attempt_at=row.next_attempt_at,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        receipt_json=row.receipt_json,
        last_error=row.last_error,
        verified_at=row.verified_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        finished_at=row.finished_at,
    )


def _attempt(row: SqlMemoryErasureAttempt) -> MemoryErasureAttempt:
    return MemoryErasureAttempt(
        id=row.id,
        workspace_id=row.workspace_id,
        task_id=row.task_id,
        attempt_number=row.attempt_number,
        status=row.status,
        error_code=row.error_code,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


class SqlAlchemyMemoryErasureStore:
    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.memory_erasure_store",
        )

    def create_request(
        self,
        *,
        workspace_id: int,
        operation_id: str,
        requested_by: str,
        scope: MemoryScope,
        provider_names: tuple[MemoryProviderName, ...],
        supported_providers: frozenset[MemoryProviderName],
        requested_at: int,
        now: int,
        defer: bool = False,
    ) -> tuple[MemoryErasure, list[MemoryErasureTask]]:
        providers = tuple(dict.fromkeys(provider_names))
        if not providers:
            raise ValueError("memory erasure requires at least one configured provider")
        if scope.workspace_id != workspace_id:
            raise ValueError("memory erasure scope workspace does not match request")
        if scope.kind != "personal" or scope.subject_id != requested_by:
            raise ValueError("personal memory erasure must target the requesting subject")
        erasure_id = _stable_id(f"memory-erasure:{workspace_id}:{operation_id}")
        with self._session("create_request") as session:
            existing = session.execute(
                select(SqlMemoryErasureRequest).where(
                    SqlMemoryErasureRequest.workspace_id == workspace_id,
                    SqlMemoryErasureRequest.operation_id == operation_id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                self._validate_replay(existing, requested_by, scope)
                return _erasure(existing), self._tasks_for_request(
                    session,
                    workspace_id,
                    existing.id,
                )
            has_unsupported = any(provider not in supported_providers for provider in providers)
            row = SqlMemoryErasureRequest(
                workspace_id=workspace_id,
                id=erasure_id,
                operation_id=operation_id,
                requested_by=requested_by,
                scope_kind=scope.kind,
                scope_subject=scope.subject_id,
                status="blocked" if has_unsupported else "pending",
                requested_at=requested_at,
                created_at=now,
                updated_at=now,
                completed_at=None,
                last_error="provider_unsupported" if has_unsupported else None,
            )
            tasks = [
                SqlMemoryErasureTask(
                    workspace_id=workspace_id,
                    id=(task_id := _stable_id(f"memory-erasure-task:{erasure_id}:{provider}")),
                    erasure_id=erasure_id,
                    provider=provider,
                    operation_id=f"memory-erasure:{task_id}",
                    status="pending" if provider in supported_providers else "unsupported",
                    attempt_count=0,
                    max_attempts=5,
                    next_attempt_at=2_147_483_647 if defer else now,
                    lease_owner=None,
                    lease_expires_at=None,
                    receipt_json=None,
                    last_error=(
                        None if provider in supported_providers else "provider_unsupported"
                    ),
                    verified_at=None,
                    created_at=now,
                    updated_at=now,
                    finished_at=(None if provider in supported_providers else now),
                )
                for provider in providers
            ]
            try:
                with session.begin_nested():
                    session.add(row)
                    session.add_all(tasks)
                    session.flush()
            except IntegrityError as exc:
                existing = session.execute(
                    select(SqlMemoryErasureRequest).where(
                        SqlMemoryErasureRequest.workspace_id == workspace_id,
                        SqlMemoryErasureRequest.operation_id == operation_id,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise
                self._validate_replay(existing, requested_by, scope, cause=exc)
                return _erasure(existing), self._tasks_for_request(
                    session,
                    workspace_id,
                    existing.id,
                )
            return _erasure(row), self._tasks_for_request(
                session,
                workspace_id,
                row.id,
            )

    def activate_request(
        self,
        *,
        workspace_id: int,
        erasure_id: str,
        now: int,
    ) -> tuple[MemoryErasure, list[MemoryErasureTask]]:
        with self._session("activate_request") as session:
            request = session.execute(
                select(SqlMemoryErasureRequest)
                .where(
                    SqlMemoryErasureRequest.workspace_id == workspace_id,
                    SqlMemoryErasureRequest.id == erasure_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if request is None:
                raise KeyError(erasure_id)
            session.execute(
                update(SqlMemoryErasureTask)
                .where(
                    SqlMemoryErasureTask.workspace_id == workspace_id,
                    SqlMemoryErasureTask.erasure_id == erasure_id,
                    SqlMemoryErasureTask.status == "pending",
                )
                .values(next_attempt_at=now, updated_at=now)
            )
            self._refresh_request(session, workspace_id, erasure_id, now)
            session.flush()
            return _erasure(request), self._tasks_for_request(
                session,
                workspace_id,
                erasure_id,
            )

    def get_request(self, workspace_id: int, erasure_id: str) -> MemoryErasure | None:
        with self._session("get_request") as session:
            row = session.get(SqlMemoryErasureRequest, (workspace_id, erasure_id))
            return _erasure(row) if row is not None else None

    def subject_is_disabled(self, workspace_id: int, requested_by: str) -> bool:
        with self._session("subject_is_disabled") as session:
            return (
                session.execute(
                    select(SqlMemoryErasureRequest.id)
                    .where(
                        SqlMemoryErasureRequest.workspace_id == workspace_id,
                        SqlMemoryErasureRequest.requested_by == requested_by,
                    )
                    .limit(1)
                ).scalar_one_or_none()
                is not None
            )

    def list_requests(
        self,
        *,
        workspace_id: int,
        requested_by: str,
        limit: int = 100,
    ) -> list[MemoryErasure]:
        with self._session("list_requests") as session:
            rows = session.execute(
                select(SqlMemoryErasureRequest)
                .where(
                    SqlMemoryErasureRequest.workspace_id == workspace_id,
                    SqlMemoryErasureRequest.requested_by == requested_by,
                )
                .order_by(
                    asc(SqlMemoryErasureRequest.created_at),
                    asc(SqlMemoryErasureRequest.id),
                )
                .limit(limit)
            ).scalars()
            return [_erasure(row) for row in rows]

    def list_tasks(self, workspace_id: int, erasure_id: str) -> list[MemoryErasureTask]:
        with self._session("list_tasks") as session:
            return self._tasks_for_request(session, workspace_id, erasure_id)

    def claim_next(
        self,
        *,
        worker_id: str,
        now: int,
        lease_seconds: int,
    ) -> MemoryErasureTask | None:
        with self._session("claim_next") as session:
            expired = list(
                session.execute(
                    select(SqlMemoryErasureTask)
                    .where(
                        SqlMemoryErasureTask.status == "processing",
                        SqlMemoryErasureTask.lease_expires_at <= now,
                    )
                    .with_for_update(skip_locked=True)
                ).scalars()
            )
            for row in expired:
                self._finish_attempt(session, row, "failed", now, "lease_expired")
                row.status = (
                    "dead_letter" if row.attempt_count >= row.max_attempts else "retryable"
                )
                row.next_attempt_at = now
                row.lease_owner = None
                row.lease_expires_at = None
                row.last_error = "lease_expired"
                row.updated_at = now
                if row.status == "dead_letter":
                    row.finished_at = now
                self._refresh_request(session, row.workspace_id, row.erasure_id, now)
            row = session.execute(
                select(SqlMemoryErasureTask)
                .where(
                    SqlMemoryErasureTask.status.in_(("pending", "retryable")),
                    SqlMemoryErasureTask.next_attempt_at <= now,
                )
                .order_by(
                    asc(SqlMemoryErasureTask.next_attempt_at),
                    asc(SqlMemoryErasureTask.created_at),
                    asc(SqlMemoryErasureTask.workspace_id),
                    asc(SqlMemoryErasureTask.id),
                )
                .limit(1)
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if row is None:
                return None
            row.status = "processing"
            row.attempt_count += 1
            row.lease_owner = worker_id
            row.lease_expires_at = now + lease_seconds
            row.updated_at = now
            session.add(
                SqlMemoryErasureAttempt(
                    workspace_id=row.workspace_id,
                    id=uuid.uuid4().hex,
                    task_id=row.id,
                    attempt_number=row.attempt_count,
                    status="running",
                    error_code=None,
                    started_at=now,
                    finished_at=None,
                )
            )
            self._refresh_request(session, row.workspace_id, row.erasure_id, now)
            session.flush()
            return _task(row)

    def complete_task(
        self,
        *,
        workspace_id: int,
        task_id: str,
        worker_id: str,
        attempt_number: int,
        receipt: dict[str, object],
        verified_at: int,
    ) -> MemoryErasureTask:
        with self._session("complete_task") as session:
            row = self._processing_task(
                session,
                workspace_id,
                task_id,
                worker_id,
                attempt_number,
            )
            row.status = "completed"
            row.receipt_json = json.dumps(receipt, separators=(",", ":"), sort_keys=True)
            row.last_error = None
            row.verified_at = verified_at
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = verified_at
            row.finished_at = verified_at
            self._finish_attempt(session, row, "completed", verified_at, None)
            self._refresh_request(session, workspace_id, row.erasure_id, verified_at)
            session.flush()
            return _task(row)

    def fail_task(
        self,
        *,
        workspace_id: int,
        task_id: str,
        worker_id: str,
        attempt_number: int,
        error_code: str,
        now: int,
    ) -> MemoryErasureTask:
        with self._session("fail_task") as session:
            row = self._processing_task(
                session,
                workspace_id,
                task_id,
                worker_id,
                attempt_number,
            )
            row.status = "dead_letter" if row.attempt_count >= row.max_attempts else "retryable"
            row.next_attempt_at = now + min(3600, 5 * (2 ** (row.attempt_count - 1)))
            row.last_error = error_code[:128]
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = now
            if row.status == "dead_letter":
                row.finished_at = now
            self._finish_attempt(session, row, "failed", now, row.last_error)
            self._refresh_request(session, workspace_id, row.erasure_id, now)
            session.flush()
            return _task(row)

    def list_attempts(self, workspace_id: int, task_id: str) -> list[MemoryErasureAttempt]:
        with self._session("list_attempts") as session:
            rows = session.execute(
                select(SqlMemoryErasureAttempt)
                .where(
                    SqlMemoryErasureAttempt.workspace_id == workspace_id,
                    SqlMemoryErasureAttempt.task_id == task_id,
                )
                .order_by(asc(SqlMemoryErasureAttempt.attempt_number))
            ).scalars()
            return [_attempt(row) for row in rows]

    @staticmethod
    def _validate_replay(
        row: SqlMemoryErasureRequest,
        requested_by: str,
        scope: MemoryScope,
        *,
        cause: BaseException | None = None,
    ) -> None:
        if (
            row.requested_by != requested_by
            or row.scope_kind != scope.kind
            or row.scope_subject not in {scope.subject_id, None}
        ):
            error = MemoryErasureStateError(
                "memory erasure operation was replayed with different context"
            )
            if cause is not None:
                raise error from cause
            raise error

    @staticmethod
    def _tasks_for_request(
        session: Any,
        workspace_id: int,
        erasure_id: str,
    ) -> list[MemoryErasureTask]:
        rows = session.execute(
            select(SqlMemoryErasureTask)
            .where(
                SqlMemoryErasureTask.workspace_id == workspace_id,
                SqlMemoryErasureTask.erasure_id == erasure_id,
            )
            .order_by(asc(SqlMemoryErasureTask.provider))
        ).scalars()
        return [_task(row) for row in rows]

    @staticmethod
    def _processing_task(
        session: Any,
        workspace_id: int,
        task_id: str,
        worker_id: str,
        attempt_number: int,
    ) -> SqlMemoryErasureTask:
        row = session.execute(
            select(SqlMemoryErasureTask)
            .where(
                SqlMemoryErasureTask.workspace_id == workspace_id,
                SqlMemoryErasureTask.id == task_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise KeyError(task_id)
        if row.status != "processing":
            raise MemoryErasureStateError("memory erasure task is not processing")
        if row.lease_owner != worker_id or row.attempt_count != attempt_number:
            raise MemoryErasureLeaseLostError("memory erasure task lease is no longer owned")
        return row

    @staticmethod
    def _finish_attempt(
        session: Any,
        task: SqlMemoryErasureTask,
        status: str,
        now: int,
        error_code: str | None,
    ) -> None:
        attempt = session.execute(
            select(SqlMemoryErasureAttempt)
            .where(
                SqlMemoryErasureAttempt.workspace_id == task.workspace_id,
                SqlMemoryErasureAttempt.task_id == task.id,
                SqlMemoryErasureAttempt.attempt_number == task.attempt_count,
            )
            .with_for_update()
        ).scalar_one()
        attempt.status = status
        attempt.error_code = error_code
        attempt.finished_at = now

    @staticmethod
    def _refresh_request(
        session: Any,
        workspace_id: int,
        erasure_id: str,
        now: int,
    ) -> None:
        request = session.execute(
            select(SqlMemoryErasureRequest)
            .where(
                SqlMemoryErasureRequest.workspace_id == workspace_id,
                SqlMemoryErasureRequest.id == erasure_id,
            )
            .with_for_update()
        ).scalar_one()
        statuses = set(
            session.execute(
                select(SqlMemoryErasureTask.status).where(
                    SqlMemoryErasureTask.workspace_id == workspace_id,
                    SqlMemoryErasureTask.erasure_id == erasure_id,
                )
            ).scalars()
        )
        request.updated_at = now
        if statuses == {"completed"}:
            request.status = "completed"
            request.scope_subject = None
            request.completed_at = now
            request.last_error = None
        elif statuses & {"unsupported", "dead_letter"}:
            request.status = "blocked"
            request.last_error = (
                "provider_unsupported" if "unsupported" in statuses else "provider_dead_letter"
            )
        else:
            request.status = "in_progress"
