from __future__ import annotations

import asyncio
import json
import time

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from omnigent.db.db_models import current_workspace_id
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.memory import MemoryErasure, MemoryErasureTask, MemoryRuntime, MemoryScope
from omnigent.memory.erasure_worker import MemoryErasureWorker
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.stores.memory_capture_store import SqlAlchemyMemoryCaptureStore
from omnigent.stores.memory_erasure_store import (
    MemoryErasureStateError,
    SqlAlchemyMemoryErasureStore,
)


class CreateMemoryErasureRequest(BaseModel):
    operation_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )

    model_config = ConfigDict(extra="forbid")


def _subject(request: Request, auth_provider: AuthProvider | None) -> str:
    user_id = get_user_id(request, auth_provider)
    if user_id is not None:
        return user_id
    if auth_provider is None:
        return RESERVED_USER_LOCAL
    raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)


def _task_response(task: MemoryErasureTask) -> dict[str, object]:
    receipt: object = None
    if task.receipt_json is not None:
        try:
            receipt = json.loads(task.receipt_json)
        except ValueError:
            receipt = None
    return {
        "provider": task.provider,
        "status": task.status,
        "attempt_count": task.attempt_count,
        "verified_at": task.verified_at,
        "last_error": task.last_error,
        "receipt": receipt,
    }


def _response(
    erasure: MemoryErasure,
    tasks: list[MemoryErasureTask],
) -> dict[str, object]:
    return {
        "id": erasure.id,
        "operation_id": erasure.operation_id,
        "scope": erasure.scope_kind,
        "status": erasure.status,
        "requested_at": erasure.requested_at,
        "completed_at": erasure.completed_at,
        "last_error": erasure.last_error,
        "providers": [_task_response(task) for task in tasks],
    }


def create_memory_erasures_router(
    store: SqlAlchemyMemoryErasureStore,
    *,
    capture_store: SqlAlchemyMemoryCaptureStore,
    memory_runtime: MemoryRuntime | None,
    auth_provider: AuthProvider | None,
    worker: MemoryErasureWorker | None,
) -> APIRouter:
    router = APIRouter()

    @router.post("/memory/erasures", status_code=202)
    async def create_memory_erasure(
        request: Request,
        body: CreateMemoryErasureRequest,
    ) -> dict[str, object]:
        subject = _subject(request, auth_provider)
        if memory_runtime is None or not memory_runtime.provider_names():
            raise OmnigentError(
                "No memory provider is configured for erasure",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        workspace_id = current_workspace_id()
        now = int(time.time())
        requested_at = int(time.time() * 1000)
        scope = MemoryScope(workspace_id, "personal", subject)
        supported = frozenset(memory_runtime.erase_providers())
        try:
            erasure, _ = await asyncio.to_thread(
                store.create_request,
                workspace_id=workspace_id,
                operation_id=body.operation_id,
                requested_by=subject,
                scope=scope,
                provider_names=memory_runtime.provider_names(),
                supported_providers=supported,
                requested_at=requested_at,
                now=now,
                defer=True,
            )
        except MemoryErasureStateError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.CONFLICT) from exc
        await asyncio.to_thread(
            capture_store.scrub_scope,
            workspace_id=workspace_id,
            scope_kind="personal",
            scope_subject=subject,
            account_subject=subject,
            now=now,
        )
        erasure, tasks = await asyncio.to_thread(
            store.activate_request,
            workspace_id=workspace_id,
            erasure_id=erasure.id,
            now=now,
        )
        if worker is not None:
            worker.wake()
        return _response(erasure, tasks)

    @router.get("/memory/erasures")
    async def list_memory_erasures(
        request: Request,
        limit: int = Query(default=100, ge=1, le=100),
    ) -> dict[str, object]:
        subject = _subject(request, auth_provider)
        workspace_id = current_workspace_id()
        erasures = await asyncio.to_thread(
            store.list_requests,
            workspace_id=workspace_id,
            requested_by=subject,
            limit=limit,
        )
        data = []
        for erasure in erasures:
            tasks = await asyncio.to_thread(store.list_tasks, workspace_id, erasure.id)
            data.append(_response(erasure, tasks))
        return {"data": data}

    @router.get("/memory/erasures/{erasure_id}")
    async def get_memory_erasure(
        request: Request,
        erasure_id: str,
    ) -> dict[str, object]:
        subject = _subject(request, auth_provider)
        workspace_id = current_workspace_id()
        erasure = await asyncio.to_thread(store.get_request, workspace_id, erasure_id)
        if erasure is None or erasure.requested_by != subject:
            raise OmnigentError("Memory erasure not found", code=ErrorCode.NOT_FOUND)
        tasks = await asyncio.to_thread(store.list_tasks, workspace_id, erasure.id)
        return _response(erasure, tasks)

    return router
