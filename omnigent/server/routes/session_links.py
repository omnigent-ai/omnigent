"""Owner communication setting and source-scoped runtime operations."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from omnigent.db.account_authority import account_generation
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.session_links import link_service


class OrchestrationSettingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    expected_version: str | None = Field(default=None, pattern=r"^[0-9a-f]{32}$")


class LinkedMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=8000)
    operation_id: str = Field(min_length=1, max_length=128)


class LinkedRequestInput(LinkedMessage):
    target_id: str


class AcceptLinkedRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str


def create_session_links_router() -> APIRouter:
    router = APIRouter()

    @router.get("/session-orchestration/settings")
    async def read_setting(request: Request) -> dict[str, Any]:
        service = link_service(request)
        user = service.owner_client(request)
        return await asyncio.to_thread(service.store.settings, user)

    @router.put("/session-orchestration/settings")
    async def change_setting(request: Request, body: OrchestrationSettingInput) -> dict[str, Any]:
        service = link_service(request)
        user = service.owner_client(request)
        return await asyncio.to_thread(
            service.store.set_enabled,
            user,
            account_generation(user),
            body.enabled,
            body.expected_version,
        )

    @router.get("/sessions/{source_id}/communication-activity")
    async def list_activity(
        request: Request,
        source_id: str,
        incoming: bool = False,
        after: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        service = link_service(request)
        identity = getattr(request.state, "link_identity", None)
        if identity is not None:
            user = service.runtime(request, source_id).requester
        else:
            user = require_user(request, service.auth)
            assert user is not None
        return await asyncio.to_thread(
            service.list_activity, source_id, user, incoming=incoming, after=after, limit=limit
        )

    @router.post("/sessions/{source_id}/linked-requests", status_code=202)
    async def delegate(
        request: Request, source_id: str, body: LinkedRequestInput
    ) -> dict[str, Any]:
        service = link_service(request)
        identity = service.runtime(request, source_id)
        return await service.delegate(
            source_id, body.target_id, identity, body.operation_id, body.message
        )

    @router.get("/sessions/{source_id}/linked-requests/{request_id}")
    async def poll(
        request: Request,
        source_id: str,
        request_id: str,
        after: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        service = link_service(request)
        identity = service.runtime(request, source_id)
        return await service.poll(source_id, request_id, identity, after=after, limit=limit)

    @router.post("/sessions/{worker_id}/linked-requests/{request_id}/replies")
    async def reply(
        request: Request, worker_id: str, request_id: str, body: LinkedMessage
    ) -> dict[str, Any]:
        service = link_service(request)
        identity = service.runtime(request, worker_id)
        return await service.reply(
            worker_id, request_id, identity, body.operation_id, body.message
        )

    @router.post("/session-links/requests/{request_id}/accept", include_in_schema=False)
    async def accept(
        request: Request, request_id: str, body: AcceptLinkedRequest
    ) -> dict[str, Any]:
        return await link_service(request).accept(request, request_id, body.session_id)

    return router
