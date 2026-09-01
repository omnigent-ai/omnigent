from __future__ import annotations

import asyncio
import time
from typing import Literal

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.db.db_models import current_workspace_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.memory import MemoryCaptureJob, MemoryCaptureReview
from omnigent.memory.capture_worker import MemoryCaptureWorker
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.stores.memory_capture_store import (
    MemoryCaptureStateError,
    SqlAlchemyMemoryCaptureStore,
)


class MemoryReviewDecision(BaseModel):
    action: Literal["approve", "reject"]
    reason: str | None = Field(default=None, max_length=512)

    model_config = ConfigDict(extra="forbid")


def _subject(request: Request, auth_provider: AuthProvider | None) -> str:
    user_id = get_user_id(request, auth_provider)
    if user_id is not None:
        return user_id
    if auth_provider is None:
        return RESERVED_USER_LOCAL
    raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)


def _response(
    review: MemoryCaptureReview,
    job: MemoryCaptureJob,
) -> dict[str, object]:
    return {
        "id": review.id,
        "job_id": job.id,
        "status": review.status,
        "provider": job.provider,
        "scope": job.scope.kind,
        "candidates": [
            {
                "kind": candidate.kind,
                "text": candidate.text,
                "confidence": candidate.confidence,
                "sensitivity": candidate.sensitivity,
                "source_item_ids": list(candidate.source_item_ids),
            }
            for candidate in review.candidates
        ],
        "created_at": review.created_at,
        "decided_at": review.decided_at,
        "decision_reason": review.decision_reason,
    }


def create_memory_reviews_router(
    store: SqlAlchemyMemoryCaptureStore,
    *,
    auth_provider: AuthProvider | None,
    worker: MemoryCaptureWorker | None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/memory/reviews")
    async def list_memory_reviews(
        request: Request,
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, object]:
        subject = _subject(request, auth_provider)
        rows = await asyncio.to_thread(
            store.list_pending_reviews,
            workspace_id=current_workspace_id(),
            requested_by=subject,
            limit=limit,
        )
        return {
            "data": [
                _response(review, job)
                for review, job in rows
                if job.scope.kind == "personal" and job.scope.subject_id == subject
            ]
        }

    @router.patch("/memory/reviews/{review_id}")
    async def decide_memory_review(
        request: Request,
        review_id: str,
        body: MemoryReviewDecision,
    ) -> dict[str, object]:
        subject = _subject(request, auth_provider)
        workspace_id = current_workspace_id()
        review = await asyncio.to_thread(store.get_review, workspace_id, review_id)
        job = (
            await asyncio.to_thread(store.get_job, workspace_id, review.job_id)
            if review is not None
            else None
        )
        if (
            review is None
            or job is None
            or review.requested_by != subject
            or job.scope.kind != "personal"
            or job.scope.subject_id != subject
        ):
            raise OmnigentError("Memory review not found", code=ErrorCode.NOT_FOUND)
        try:
            decided, updated_job = await asyncio.to_thread(
                store.decide_review,
                workspace_id=workspace_id,
                review_id=review_id,
                decision="approved" if body.action == "approve" else "rejected",
                decided_by=subject,
                reason=body.reason,
                now=int(time.time()),
            )
        except MemoryCaptureStateError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.CONFLICT) from exc
        if body.action == "approve" and worker is not None:
            worker.wake()
        return _response(decided, updated_job)

    return router
