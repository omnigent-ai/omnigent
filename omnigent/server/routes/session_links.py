"""Human link administration and source-scoped runtime operations."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from omnigent.db.account_authority import account_generation
from omnigent.server.session_links import link_service


class LinkApproval(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_id: str


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

    @router.get("/sessions/{source_id}/links")
    async def list_links(
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
            user = service.human(request)
        return await asyncio.to_thread(
            service.list_links, source_id, user, incoming=incoming, after=after, limit=limit
        )

    @router.post("/sessions/{source_id}/links", status_code=201)
    async def connect(request: Request, source_id: str, body: LinkApproval) -> dict[str, Any]:
        service = link_service(request)
        user = service.human(request)
        _, source = await asyncio.to_thread(service.endpoint, source_id)
        _, target = await asyncio.to_thread(service.endpoint, body.target_id)
        link = await asyncio.to_thread(
            service.store.connect, source, target, user, account_generation(user)
        )
        return {key: value for key, value in link.items() if key != "approver_generation"}

    @router.delete("/sessions/{source_id}/links/{target_id}", status_code=204)
    async def disconnect(request: Request, source_id: str, target_id: str) -> Response:
        service = link_service(request)
        user = service.human(request)
        _, source = await asyncio.to_thread(service.endpoint, source_id, allow_archived=True)
        _, target = await asyncio.to_thread(service.endpoint, target_id, allow_archived=True)
        await service.disconnect(source, target, user)
        return Response(status_code=204)

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
