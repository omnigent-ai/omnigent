from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from omnigent.entities import DpiaCaseRecord, DpiaCaseRevision
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.dpia_case_store import DpiaCaseConflictError, DpiaCaseStore
from omnigent.stores.permission_store import PermissionStore


class SaveDpiaCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=0)
    snapshot: dict[str, Any]


def _case_response(value: DpiaCaseRecord) -> dict[str, Any]:
    return {
        "case_id": value.case_id,
        "revision": value.revision,
        "snapshot": value.snapshot,
        "created_by": value.created_by,
        "updated_by": value.updated_by,
        "created_at": value.created_at,
        "updated_at": value.updated_at,
    }


def _revision_response(value: DpiaCaseRevision, *, include_snapshot: bool) -> dict[str, Any]:
    response: dict[str, Any] = {
        "case_id": value.case_id,
        "revision": value.revision,
        "actor": value.actor,
        "created_at": value.created_at,
    }
    if include_snapshot:
        response["snapshot"] = value.snapshot
    return response


def create_dpia_cases_router(
    store: DpiaCaseStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    admin_list: Any | None = None,
) -> APIRouter:
    router = APIRouter()

    def actor(request: Request) -> str:
        return require_user(request, auth_provider) or RESERVED_USER_LOCAL

    async def require_editor(request: Request) -> str:
        user_id = actor(request)
        if permission_store is None:
            return user_id
        is_admin = await asyncio.to_thread(permission_store.is_admin, user_id)
        if admin_list is not None:
            is_admin = is_admin or admin_list.is_admin(user_id)
        if not is_admin:
            raise OmnigentError(
                "Admin privileges required to update DPIA cases",
                code=ErrorCode.FORBIDDEN,
            )
        return user_id

    @router.get("/dpia/cases")
    async def list_cases(request: Request) -> dict[str, Any]:
        actor(request)
        cases = await asyncio.to_thread(store.list_cases)
        return {"cases": [_case_response(case) for case in cases]}

    @router.get("/dpia/cases/{case_id}")
    async def get_case(case_id: str, request: Request) -> dict[str, Any]:
        actor(request)
        case = await asyncio.to_thread(store.get_case, case_id)
        if case is None:
            raise OmnigentError("DPIA case not found", code=ErrorCode.NOT_FOUND)
        return _case_response(case)

    @router.put("/dpia/cases/{case_id}")
    async def save_case(
        case_id: str,
        request: Request,
        body: SaveDpiaCaseRequest,
    ) -> JSONResponse:
        user_id = await require_editor(request)
        try:
            saved = await asyncio.to_thread(
                store.save_case,
                case_id,
                body.snapshot,
                expected_revision=body.expected_revision,
                actor=user_id,
            )
        except DpiaCaseConflictError as exc:
            return JSONResponse(
                status_code=409,
                content={
                    "error": {
                        "code": ErrorCode.CONFLICT,
                        "message": "DPIA case changed since it was loaded",
                        "current_revision": exc.current_revision,
                    }
                },
            )
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        return JSONResponse(content=_case_response(saved))

    @router.get("/dpia/cases/{case_id}/revisions")
    async def list_revisions(
        case_id: str,
        request: Request,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> dict[str, Any]:
        actor(request)
        if await asyncio.to_thread(store.get_case, case_id) is None:
            raise OmnigentError("DPIA case not found", code=ErrorCode.NOT_FOUND)
        revisions = await asyncio.to_thread(store.list_revisions, case_id, limit=limit)
        return {
            "case_id": case_id,
            "revisions": [
                _revision_response(revision, include_snapshot=False) for revision in revisions
            ],
        }

    @router.get("/dpia/cases/{case_id}/revisions/{revision}")
    async def get_revision(case_id: str, revision: int, request: Request) -> dict[str, Any]:
        actor(request)
        value = await asyncio.to_thread(store.get_revision, case_id, revision)
        if value is None:
            raise OmnigentError("DPIA case revision not found", code=ErrorCode.NOT_FOUND)
        return _revision_response(value, include_snapshot=True)

    return router
