"""Owner-private, project-scoped ticket APIs. There is intentionally no delete route."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import APIRouter, Query, Request
from sqlalchemy.exc import StatementError

from omnigent.entities.project_ticket import (
    CommentOnTicket,
    CreateTicket,
    LinkRelatedTicket,
    LinkTicketSession,
    TicketActor,
    TicketEventKind,
    TicketStatus,
    UpdateTicket,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.server.auth import LEVEL_EDIT, AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.session_orchestration import require_named_source_private
from omnigent.stores import ConversationStore, PermissionStore
from omnigent.stores.project_ticket_store import ProjectTicketStore
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes


@contextmanager
def _storage_errors() -> Iterator[None]:
    try:
        yield
    except StatementError as exc:
        if not isinstance(exc.orig, ValueError):
            raise
        raise OmnigentError(
            "Ticket content exceeds its storage limit", code=ErrorCode.INVALID_INPUT
        ) from exc


def create_project_tickets_router(
    store: ProjectTicketStore,
    conversations: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
    permissions: PermissionStore | None = None,
) -> APIRouter:
    router = APIRouter(prefix="/projects/{project_id}/tickets")

    async def actor(request: Request, project_id: str) -> TicketActor:
        user_id = require_user(request, auth_provider)
        raw_source = request.headers.get(ORCHESTRATION_SOURCE_HEADER)
        if raw_source is None:
            return TicketActor(user_id)
        await asyncio.to_thread(
            require_named_source_private, request, user_id, conversations, permissions
        )
        try:
            source_id = uuid_to_bytes(raw_source).hex()
            project = uuid_to_bytes(project_id).hex()
        except InvalidUuidError as exc:
            raise OmnigentError(
                "Invalid calling session or project", code=ErrorCode.FORBIDDEN
            ) from exc
        source = await asyncio.to_thread(conversations.get_conversation, source_id)
        if source is None or source.project_id != project or source.archived:
            raise OmnigentError(
                "Ticket tools are scoped to their calling session's project",
                code=ErrorCode.FORBIDDEN,
            )
        return TicketActor(user_id, source.id, source.agent_id)

    def wake(request: Request) -> None:
        notifications = getattr(request.app.state, "project_ticket_notifications", None)
        if notifications is not None:
            notifications.wake()

    async def check_session(
        who: TicketActor, project_id: str, session_id: str, *, require_active: bool = False
    ) -> str:
        try:
            session_id = uuid_to_bytes(session_id).hex()
        except InvalidUuidError as exc:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND) from exc
        await require_access(who.user_id, session_id, LEVEL_EDIT, permissions, conversations)
        target = await asyncio.to_thread(conversations.get_conversation, session_id)
        if target is None or target.project_id != uuid_to_bytes(project_id).hex():
            raise OmnigentError(
                "Ticket and session must belong to the same project",
                code=ErrorCode.INVALID_INPUT,
            )
        if require_active and target.archived:
            raise OmnigentError(
                "Archived sessions cannot lead a ticket", code=ErrorCode.INVALID_INPUT
            )
        return session_id

    @router.get("")
    async def list_tickets(
        request: Request,
        project_id: str,
        status: TicketStatus | None = None,
        after: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        return await asyncio.to_thread(
            store.list, project_id, user_id=who.user_id, status=status, after=after, limit=limit
        )

    @router.post("", status_code=201)
    async def create_ticket(
        request: Request, project_id: str, body: CreateTicket
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        with _storage_errors():
            ticket = await asyncio.to_thread(store.create, project_id, body, who)
        wake(request)
        return ticket

    @router.get("/{ticket_id}")
    async def get_ticket(request: Request, project_id: str, ticket_id: str) -> dict[str, Any]:
        who = await actor(request, project_id)
        return await asyncio.to_thread(store.get, project_id, ticket_id, user_id=who.user_id)

    @router.patch("/{ticket_id}")
    async def update_ticket(
        request: Request, project_id: str, ticket_id: str, body: UpdateTicket
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        if body.lead_session_id is not None:
            await asyncio.to_thread(store.get, project_id, ticket_id, user_id=who.user_id)
            await check_session(who, project_id, body.lead_session_id, require_active=True)
        with _storage_errors():
            ticket = await asyncio.to_thread(store.update, project_id, ticket_id, body, who)
        wake(request)
        return ticket

    @router.post("/{ticket_id}/comments", status_code=201)
    async def comment(
        request: Request, project_id: str, ticket_id: str, body: CommentOnTicket
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        with _storage_errors():
            ticket = await asyncio.to_thread(store.comment, project_id, ticket_id, body, who)
        wake(request)
        return ticket

    @router.get("/{ticket_id}/events")
    async def events(
        request: Request,
        project_id: str,
        ticket_id: str,
        after: int = Query(-1, ge=-1),
        limit: int = Query(50, ge=1, le=100),
        kind: TicketEventKind | None = None,
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        return await asyncio.to_thread(
            store.events,
            project_id,
            ticket_id,
            user_id=who.user_id,
            after=after,
            limit=limit,
            kind=kind,
        )

    @router.get("/{ticket_id}/sessions")
    async def sessions(
        request: Request,
        project_id: str,
        ticket_id: str,
        after: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        return await asyncio.to_thread(
            store.sessions, project_id, ticket_id, user_id=who.user_id, after=after, limit=limit
        )

    @router.post("/{ticket_id}/sessions")
    async def link_session(
        request: Request, project_id: str, ticket_id: str, body: LinkTicketSession
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        await asyncio.to_thread(store.get, project_id, ticket_id, user_id=who.user_id)
        session_id = await check_session(who, project_id, body.session_id)
        result = await asyncio.to_thread(
            store.link_session, project_id, ticket_id, session_id, body.action, who
        )
        wake(request)
        return result

    @router.get("/{ticket_id}/related")
    async def related_tickets(
        request: Request,
        project_id: str,
        ticket_id: str,
        after: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        return await asyncio.to_thread(
            store.related, project_id, ticket_id, user_id=who.user_id, after=after, limit=limit
        )

    @router.post("/{ticket_id}/related")
    async def link_related_ticket(
        request: Request,
        project_id: str,
        ticket_id: str,
        body: LinkRelatedTicket,
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        with _storage_errors():
            result = await asyncio.to_thread(
                store.link_ticket, project_id, ticket_id, body.related_ticket_id, body.action, who
            )
        wake(request)
        return result

    return router
