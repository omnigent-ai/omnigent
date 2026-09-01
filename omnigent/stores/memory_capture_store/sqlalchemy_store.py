from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from typing import Any, cast

from sqlalchemy import asc, select, update
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import (
    SqlMemoryCaptureAttempt,
    SqlMemoryCaptureIntent,
    SqlMemoryCaptureJob,
    SqlMemoryCaptureReview,
)
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker
from omnigent.memory.capture_models import (
    MemoryCandidate,
    MemoryCaptureAttempt,
    MemoryCaptureIntent,
    MemoryCaptureJob,
    MemoryCaptureMode,
    MemoryCapturePhase,
    MemoryCaptureReview,
    MemoryCaptureReviewStatus,
    MemoryCaptureTarget,
)
from omnigent.memory.models import MemoryScope
from omnigent.spec.types import MemoryProviderName, MemoryScopeKind


class MemoryCaptureStateError(RuntimeError):
    pass


class MemoryCaptureLeaseLostError(MemoryCaptureStateError):
    pass


def _json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _stable_id(value: str) -> str:
    return _digest(value)[:32]


def _target_dict(target: MemoryCaptureTarget) -> dict[str, object]:
    return {
        "capture_mode": target.capture_mode,
        "policy_hash": target.policy_hash,
        "provider": target.provider,
        "scope_kind": target.scope.kind,
        "scope_subject": target.scope.subject_id,
    }


def _target_from_dict(workspace_id: int, value: dict[str, Any]) -> MemoryCaptureTarget:
    return MemoryCaptureTarget(
        provider=cast(MemoryProviderName, value["provider"]),
        scope=MemoryScope(
            workspace_id=workspace_id,
            kind=cast(MemoryScopeKind, value["scope_kind"]),
            subject_id=cast(str | None, value.get("scope_subject")),
        ),
        capture_mode=cast(MemoryCaptureMode, value["capture_mode"]),
        policy_hash=str(value["policy_hash"]),
    )


def _intent(row: SqlMemoryCaptureIntent) -> MemoryCaptureIntent:
    values = json.loads(row.targets_json)
    return MemoryCaptureIntent(
        id=row.id,
        workspace_id=row.workspace_id,
        source_item_id=row.source_item_id,
        conversation_id=row.conversation_id,
        account_subject=row.account_subject,
        targets=tuple(_target_from_dict(row.workspace_id, value) for value in values),
        targets_hash=row.targets_hash,
        status=row.status,
        response_id=row.response_id,
        created_at=row.created_at,
        expires_at=row.expires_at,
        completed_at=row.completed_at,
    )


def _job(row: SqlMemoryCaptureJob) -> MemoryCaptureJob:
    return MemoryCaptureJob(
        id=row.id,
        workspace_id=row.workspace_id,
        intent_id=row.intent_id,
        conversation_id=row.conversation_id,
        response_id=row.response_id,
        source_item_id=row.source_item_id,
        account_subject=row.account_subject,
        provider=cast(MemoryProviderName, row.provider),
        scope=MemoryScope(
            workspace_id=row.workspace_id,
            kind=cast(MemoryScopeKind, row.scope_kind),
            subject_id=row.scope_subject,
        ),
        capture_mode=cast(MemoryCaptureMode, row.capture_mode),
        policy_hash=row.policy_hash,
        policy_version=row.policy_version,
        phase=cast(MemoryCapturePhase, row.phase),
        status=cast(Any, row.status),
        operation_id=row.operation_id,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        next_attempt_at=row.next_attempt_at,
        lease_owner=row.lease_owner,
        lease_expires_at=row.lease_expires_at,
        payload_hash=row.payload_hash,
        last_error=row.last_error,
        receipt_json=row.receipt_json,
        created_at=row.created_at,
        updated_at=row.updated_at,
        finished_at=row.finished_at,
    )


def _candidate(value: dict[str, Any]) -> MemoryCandidate:
    return MemoryCandidate(
        kind=value["kind"],
        text=value["text"],
        confidence=float(value["confidence"]),
        sensitivity=value["sensitivity"],
        source_item_ids=tuple(value["source_item_ids"]),
    )


def _review(row: SqlMemoryCaptureReview) -> MemoryCaptureReview:
    values = json.loads(row.candidates_json)
    return MemoryCaptureReview(
        id=row.id,
        workspace_id=row.workspace_id,
        job_id=row.job_id,
        status=cast(MemoryCaptureReviewStatus, row.status),
        candidates=tuple(_candidate(value) for value in values),
        requested_by=row.requested_by,
        decided_by=row.decided_by,
        decision_reason=row.decision_reason,
        created_at=row.created_at,
        decided_at=row.decided_at,
    )


def _attempt(row: SqlMemoryCaptureAttempt) -> MemoryCaptureAttempt:
    return MemoryCaptureAttempt(
        id=row.id,
        workspace_id=row.workspace_id,
        job_id=row.job_id,
        attempt_number=row.attempt_number,
        phase=cast(MemoryCapturePhase, row.phase),
        status=row.status,
        error=row.error,
        receipt_json=row.receipt_json,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


class SqlAlchemyMemoryCaptureStore:
    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.memory_capture_store",
        )

    def register_intent(
        self,
        *,
        workspace_id: int,
        source_item_id: str,
        conversation_id: str,
        account_subject: str,
        targets: tuple[MemoryCaptureTarget, ...],
        now: int,
        expires_at: int,
    ) -> MemoryCaptureIntent:
        if not targets:
            raise ValueError("memory capture intent requires at least one target")
        if any(target.scope.workspace_id != workspace_id for target in targets):
            raise ValueError("memory capture target workspace does not match intent")
        if any(
            target.scope.kind == "personal" and target.scope.subject_id != account_subject
            for target in targets
        ):
            raise ValueError("personal memory capture target does not match account subject")
        target_values = [_target_dict(target) for target in targets]
        targets_json = _json(target_values)
        targets_hash = _digest(targets_json)
        intent_id = _stable_id(f"intent:{workspace_id}:{source_item_id}")
        with self._session("register_intent") as session:
            existing = session.execute(
                select(SqlMemoryCaptureIntent).where(
                    SqlMemoryCaptureIntent.workspace_id == workspace_id,
                    SqlMemoryCaptureIntent.source_item_id == source_item_id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                if (
                    existing.conversation_id != conversation_id
                    or existing.account_subject != account_subject
                    or existing.targets_hash != targets_hash
                ):
                    raise MemoryCaptureStateError(
                        "memory capture intent replayed with different context"
                    )
                return _intent(existing)
            row = SqlMemoryCaptureIntent(
                workspace_id=workspace_id,
                id=intent_id,
                source_item_id=source_item_id,
                conversation_id=conversation_id,
                account_subject=account_subject,
                targets_json=targets_json,
                targets_hash=targets_hash,
                status="pending",
                response_id=None,
                created_at=now,
                expires_at=expires_at,
                completed_at=None,
            )
            try:
                with session.begin_nested():
                    session.add(row)
                    session.flush()
            except IntegrityError as exc:
                existing = session.execute(
                    select(SqlMemoryCaptureIntent).where(
                        SqlMemoryCaptureIntent.workspace_id == workspace_id,
                        SqlMemoryCaptureIntent.source_item_id == source_item_id,
                    )
                ).scalar_one()
                if (
                    existing.conversation_id != conversation_id
                    or existing.account_subject != account_subject
                    or existing.targets_hash != targets_hash
                ):
                    raise MemoryCaptureStateError(
                        "memory capture intent replayed with different context"
                    ) from exc
                return _intent(existing)
            return _intent(row)

    def complete_intent(
        self,
        *,
        workspace_id: int,
        intent_id: str,
        source_item_id: str,
        response_id: str,
        now: int,
    ) -> list[MemoryCaptureJob]:
        with self._session("complete_intent") as session:
            row = session.execute(
                select(SqlMemoryCaptureIntent)
                .where(
                    SqlMemoryCaptureIntent.workspace_id == workspace_id,
                    SqlMemoryCaptureIntent.id == intent_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                return []
            if row.source_item_id != source_item_id:
                raise MemoryCaptureStateError("memory capture source item does not match intent")
            if row.status == "cancelled":
                return []
            if row.status == "completed" and row.response_id != response_id:
                raise MemoryCaptureStateError(
                    "memory capture intent replayed with a different response"
                )
            if row.status == "pending":
                targets = _intent(row).targets
                for target in targets:
                    target_json = _json(_target_dict(target))
                    target_hash = _digest(target_json)
                    job_id = _stable_id(
                        f"job:{workspace_id}:{intent_id}:{target.provider}:{target_hash}"
                    )
                    session.add(
                        SqlMemoryCaptureJob(
                            workspace_id=workspace_id,
                            id=job_id,
                            intent_id=intent_id,
                            conversation_id=row.conversation_id,
                            response_id=response_id,
                            source_item_id=source_item_id,
                            account_subject=row.account_subject,
                            provider=target.provider,
                            scope_kind=target.scope.kind,
                            scope_subject=target.scope.subject_id,
                            target_hash=target_hash,
                            capture_mode=target.capture_mode,
                            policy_hash=target.policy_hash,
                            policy_version=1,
                            phase="extraction",
                            status="pending",
                            operation_id=f"memory-capture:{job_id}",
                            attempt_count=0,
                            max_attempts=5,
                            next_attempt_at=now,
                            lease_owner=None,
                            lease_expires_at=None,
                            payload_hash=None,
                            last_error=None,
                            receipt_json=None,
                            created_at=now,
                            updated_at=now,
                            finished_at=None,
                        )
                    )
                row.status = "completed"
                row.response_id = response_id
                row.completed_at = now
                session.flush()
            jobs = session.execute(
                select(SqlMemoryCaptureJob)
                .where(
                    SqlMemoryCaptureJob.workspace_id == workspace_id,
                    SqlMemoryCaptureJob.intent_id == intent_id,
                )
                .order_by(asc(SqlMemoryCaptureJob.provider), asc(SqlMemoryCaptureJob.id))
            ).scalars()
            return [_job(job) for job in jobs]

    def expire_intents(self, now: int) -> int:
        with self._session("expire_intents") as session:
            result = session.execute(
                update(SqlMemoryCaptureIntent)
                .where(
                    SqlMemoryCaptureIntent.status == "pending",
                    SqlMemoryCaptureIntent.expires_at <= now,
                )
                .values(status="cancelled")
            )
            return cast(Any, result).rowcount or 0

    def scrub_scope(
        self,
        *,
        workspace_id: int,
        scope_kind: str,
        scope_subject: str | None,
        account_subject: str,
        now: int,
    ) -> int:
        with self._session("scrub_scope") as session:
            job_ids = list(
                session.execute(
                    select(SqlMemoryCaptureJob.id).where(
                        SqlMemoryCaptureJob.workspace_id == workspace_id,
                        SqlMemoryCaptureJob.scope_kind == scope_kind,
                        SqlMemoryCaptureJob.scope_subject == scope_subject,
                    )
                ).scalars()
            )
            if job_ids:
                session.execute(
                    update(SqlMemoryCaptureAttempt)
                    .where(
                        SqlMemoryCaptureAttempt.workspace_id == workspace_id,
                        SqlMemoryCaptureAttempt.job_id.in_(job_ids),
                    )
                    .values(error=None, receipt_json=None)
                )
                session.execute(
                    update(SqlMemoryCaptureAttempt)
                    .where(
                        SqlMemoryCaptureAttempt.workspace_id == workspace_id,
                        SqlMemoryCaptureAttempt.job_id.in_(job_ids),
                        SqlMemoryCaptureAttempt.status == "running",
                    )
                    .values(status="failed", finished_at=now)
                )
                session.execute(
                    update(SqlMemoryCaptureReview)
                    .where(
                        SqlMemoryCaptureReview.workspace_id == workspace_id,
                        SqlMemoryCaptureReview.job_id.in_(job_ids),
                    )
                    .values(
                        status="cancelled",
                        candidates_json="[]",
                        requested_by="",
                        decided_by=None,
                        decision_reason=None,
                        decided_at=now,
                    )
                )
                session.execute(
                    update(SqlMemoryCaptureJob)
                    .where(
                        SqlMemoryCaptureJob.workspace_id == workspace_id,
                        SqlMemoryCaptureJob.id.in_(job_ids),
                    )
                    .values(
                        scope_subject="__erased__",
                        account_subject="",
                        status="cancelled",
                        lease_owner=None,
                        lease_expires_at=None,
                        payload_hash=None,
                        last_error=None,
                        receipt_json=None,
                        updated_at=now,
                        finished_at=now,
                    )
                )
            session.execute(
                update(SqlMemoryCaptureIntent)
                .where(
                    SqlMemoryCaptureIntent.workspace_id == workspace_id,
                    SqlMemoryCaptureIntent.account_subject == account_subject,
                )
                .values(
                    account_subject="",
                    targets_json="[]",
                    status="cancelled",
                    completed_at=now,
                )
            )
            return len(job_ids)

    def cancel_intent(
        self,
        *,
        workspace_id: int,
        intent_id: str,
        source_item_id: str,
    ) -> bool:
        with self._session("cancel_intent") as session:
            row = session.execute(
                select(SqlMemoryCaptureIntent)
                .where(
                    SqlMemoryCaptureIntent.workspace_id == workspace_id,
                    SqlMemoryCaptureIntent.id == intent_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                return False
            if row.source_item_id != source_item_id:
                raise MemoryCaptureStateError("memory capture source item does not match intent")
            if row.status == "completed":
                return False
            if row.status == "pending":
                row.status = "cancelled"
            return True

    def claim_next(
        self,
        *,
        worker_id: str,
        now: int,
        lease_seconds: int,
    ) -> MemoryCaptureJob | None:
        with self._session("claim_next_job") as session:
            expired = session.execute(
                select(SqlMemoryCaptureJob)
                .where(
                    SqlMemoryCaptureJob.status == "processing",
                    SqlMemoryCaptureJob.lease_expires_at <= now,
                )
                .with_for_update(skip_locked=True)
            ).scalars()
            for row in expired:
                self._finish_attempt(
                    session,
                    row,
                    "failed",
                    now,
                    error="worker lease expired",
                )
                row.status = (
                    "dead_letter" if row.attempt_count >= row.max_attempts else "retryable"
                )
                row.next_attempt_at = now
                row.lease_owner = None
                row.lease_expires_at = None
                row.updated_at = now
                if row.status == "dead_letter":
                    row.finished_at = now

            row = session.execute(
                select(SqlMemoryCaptureJob)
                .where(
                    SqlMemoryCaptureJob.status.in_(("pending", "retryable")),
                    SqlMemoryCaptureJob.next_attempt_at <= now,
                )
                .order_by(
                    asc(SqlMemoryCaptureJob.next_attempt_at),
                    asc(SqlMemoryCaptureJob.created_at),
                    asc(SqlMemoryCaptureJob.workspace_id),
                    asc(SqlMemoryCaptureJob.id),
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
                SqlMemoryCaptureAttempt(
                    workspace_id=row.workspace_id,
                    id=uuid.uuid4().hex,
                    job_id=row.id,
                    attempt_number=row.attempt_count,
                    phase=row.phase,
                    status="running",
                    error=None,
                    receipt_json=None,
                    started_at=now,
                    finished_at=None,
                )
            )
            session.flush()
            return _job(row)

    def complete_extraction(
        self,
        *,
        workspace_id: int,
        job_id: str,
        worker_id: str,
        attempt_number: int,
        candidates: tuple[MemoryCandidate, ...],
        now: int,
    ) -> MemoryCaptureReview | None:
        candidates_json = _json([asdict(candidate) for candidate in candidates])
        payload_hash = _digest(candidates_json)
        with self._session("complete_extraction") as session:
            row = self._processing_job(
                session,
                workspace_id,
                job_id,
                "extraction",
                worker_id,
                attempt_number,
            )
            row.payload_hash = payload_hash
            row.lease_owner = None
            row.lease_expires_at = None
            row.last_error = None
            row.updated_at = now
            receipt = _json({"candidates": len(candidates), "payload_hash": payload_hash})
            self._finish_attempt(session, row, "succeeded", now, receipt_json=receipt)
            if not candidates:
                row.status = "succeeded"
                row.receipt_json = receipt
                row.finished_at = now
                return None
            if row.capture_mode == "automatic":
                row.phase = "write"
                row.status = "pending"
                row.next_attempt_at = now
                return None
            review_id = _stable_id(f"review:{workspace_id}:{job_id}")
            review = SqlMemoryCaptureReview(
                workspace_id=workspace_id,
                id=review_id,
                job_id=job_id,
                status="pending",
                candidates_json=candidates_json,
                requested_by=row.account_subject,
                decided_by=None,
                decision_reason=None,
                created_at=now,
                decided_at=None,
            )
            session.add(review)
            row.status = "pending_review"
            session.flush()
            return _review(review)

    def decide_review(
        self,
        *,
        workspace_id: int,
        review_id: str,
        decision: str,
        decided_by: str,
        reason: str | None,
        now: int,
    ) -> tuple[MemoryCaptureReview, MemoryCaptureJob]:
        if decision not in {"approved", "rejected"}:
            raise ValueError("decision must be approved or rejected")
        with self._session("decide_review") as session:
            review = session.execute(
                select(SqlMemoryCaptureReview)
                .where(
                    SqlMemoryCaptureReview.workspace_id == workspace_id,
                    SqlMemoryCaptureReview.id == review_id,
                )
                .with_for_update()
            ).scalar_one_or_none()
            if review is None:
                raise KeyError(review_id)
            job = session.execute(
                select(SqlMemoryCaptureJob)
                .where(
                    SqlMemoryCaptureJob.workspace_id == workspace_id,
                    SqlMemoryCaptureJob.id == review.job_id,
                )
                .with_for_update()
            ).scalar_one()
            if review.status != "pending" or job.status != "pending_review":
                raise MemoryCaptureStateError("memory capture review is already decided")
            review.status = decision
            review.decided_by = decided_by
            review.decision_reason = reason
            review.decided_at = now
            job.lease_owner = None
            job.lease_expires_at = None
            job.updated_at = now
            if decision == "approved":
                job.phase = "write"
                job.status = "pending"
                job.next_attempt_at = now
            else:
                job.status = "cancelled"
                job.finished_at = now
            session.flush()
            return _review(review), _job(job)

    def complete_write(
        self,
        *,
        workspace_id: int,
        job_id: str,
        worker_id: str,
        attempt_number: int,
        receipt: dict[str, object],
        now: int,
    ) -> MemoryCaptureJob:
        receipt_json = _json(receipt)
        with self._session("complete_write") as session:
            row = self._processing_job(
                session,
                workspace_id,
                job_id,
                "write",
                worker_id,
                attempt_number,
            )
            row.status = "succeeded"
            row.receipt_json = receipt_json
            row.last_error = None
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = now
            row.finished_at = now
            self._finish_attempt(session, row, "succeeded", now, receipt_json=receipt_json)
            session.flush()
            return _job(row)

    def fail_job(
        self,
        *,
        workspace_id: int,
        job_id: str,
        worker_id: str,
        attempt_number: int,
        error: str,
        now: int,
    ) -> MemoryCaptureJob:
        with self._session("fail_job") as session:
            row = self._processing_job(
                session,
                workspace_id,
                job_id,
                None,
                worker_id,
                attempt_number,
            )
            message = error[:4000]
            row.status = "dead_letter" if row.attempt_count >= row.max_attempts else "retryable"
            row.next_attempt_at = now + min(3600, 5 * (2 ** (row.attempt_count - 1)))
            row.last_error = message
            row.lease_owner = None
            row.lease_expires_at = None
            row.updated_at = now
            if row.status == "dead_letter":
                row.finished_at = now
            self._finish_attempt(session, row, "failed", now, error=message)
            session.flush()
            return _job(row)

    def get_job(self, workspace_id: int, job_id: str) -> MemoryCaptureJob | None:
        with self._session("get_job") as session:
            row = session.get(SqlMemoryCaptureJob, (workspace_id, job_id))
            return _job(row) if row is not None else None

    def get_intent(self, workspace_id: int, intent_id: str) -> MemoryCaptureIntent | None:
        with self._session("get_intent") as session:
            row = session.get(SqlMemoryCaptureIntent, (workspace_id, intent_id))
            return _intent(row) if row is not None else None

    def get_review(self, workspace_id: int, review_id: str) -> MemoryCaptureReview | None:
        with self._session("get_review") as session:
            row = session.get(SqlMemoryCaptureReview, (workspace_id, review_id))
            return _review(row) if row is not None else None

    def get_review_for_job(
        self,
        workspace_id: int,
        job_id: str,
    ) -> MemoryCaptureReview | None:
        with self._session("get_review_for_job") as session:
            row = session.execute(
                select(SqlMemoryCaptureReview).where(
                    SqlMemoryCaptureReview.workspace_id == workspace_id,
                    SqlMemoryCaptureReview.job_id == job_id,
                )
            ).scalar_one_or_none()
            return _review(row) if row is not None else None

    def list_pending_reviews(
        self,
        *,
        workspace_id: int,
        requested_by: str,
        limit: int = 100,
    ) -> list[tuple[MemoryCaptureReview, MemoryCaptureJob]]:
        with self._session("list_pending_reviews") as session:
            rows = session.execute(
                select(SqlMemoryCaptureReview, SqlMemoryCaptureJob)
                .join(
                    SqlMemoryCaptureJob,
                    (SqlMemoryCaptureJob.workspace_id == SqlMemoryCaptureReview.workspace_id)
                    & (SqlMemoryCaptureJob.id == SqlMemoryCaptureReview.job_id),
                )
                .where(
                    SqlMemoryCaptureReview.workspace_id == workspace_id,
                    SqlMemoryCaptureReview.status == "pending",
                    SqlMemoryCaptureReview.requested_by == requested_by,
                )
                .order_by(
                    asc(SqlMemoryCaptureReview.created_at),
                    asc(SqlMemoryCaptureReview.id),
                )
                .limit(limit)
            ).all()
            return [(_review(review), _job(job)) for review, job in rows]

    def list_attempts(self, workspace_id: int, job_id: str) -> list[MemoryCaptureAttempt]:
        with self._session("list_attempts") as session:
            rows = session.execute(
                select(SqlMemoryCaptureAttempt)
                .where(
                    SqlMemoryCaptureAttempt.workspace_id == workspace_id,
                    SqlMemoryCaptureAttempt.job_id == job_id,
                )
                .order_by(asc(SqlMemoryCaptureAttempt.attempt_number))
            ).scalars()
            return [_attempt(row) for row in rows]

    @staticmethod
    def _processing_job(
        session: Any,
        workspace_id: int,
        job_id: str,
        phase: str | None = None,
        worker_id: str | None = None,
        attempt_number: int | None = None,
    ) -> SqlMemoryCaptureJob:
        row = session.execute(
            select(SqlMemoryCaptureJob)
            .where(
                SqlMemoryCaptureJob.workspace_id == workspace_id,
                SqlMemoryCaptureJob.id == job_id,
            )
            .with_for_update()
        ).scalar_one_or_none()
        if row is None:
            raise KeyError(job_id)
        if row.status != "processing" or (phase is not None and row.phase != phase):
            raise MemoryCaptureStateError("memory capture job is not processing this phase")
        if (
            worker_id is not None
            and attempt_number is not None
            and (row.lease_owner != worker_id or row.attempt_count != attempt_number)
        ):
            raise MemoryCaptureLeaseLostError("memory capture job lease is no longer owned")
        return row

    @staticmethod
    def _finish_attempt(
        session: Any,
        job: SqlMemoryCaptureJob,
        status: str,
        now: int,
        *,
        error: str | None = None,
        receipt_json: str | None = None,
    ) -> None:
        attempt = session.execute(
            select(SqlMemoryCaptureAttempt)
            .where(
                SqlMemoryCaptureAttempt.workspace_id == job.workspace_id,
                SqlMemoryCaptureAttempt.job_id == job.id,
                SqlMemoryCaptureAttempt.attempt_number == job.attempt_count,
            )
            .with_for_update()
        ).scalar_one()
        attempt.status = status
        attempt.error = error
        attempt.receipt_json = receipt_json
        attempt.finished_at = now
