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
    MarkTicketSeen,
    TicketActor,
    TicketEventKind,
    TicketStageId,
    UpdateTicket,
    UpdateTicketStages,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runtime.agent_cache import AgentCache
from omnigent.runtime.project_ticket_instructions import PROJECT_TICKET_WORK_INSTRUCTION
from omnigent.server.auth import LEVEL_EDIT, RESERVED_USER_PUBLIC, AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._sessions.common import _session_status_cache
from omnigent.server.routes._sessions.helpers import (
    _permission_level_from_grants,
    _resolve_harness,
)
from omnigent.server import session_live_state
from omnigent.server.session_orchestration import require_named_source_private
from omnigent.stores import AgentStore, ConversationStore, PermissionStore
from omnigent.stores.conversation_store import SessionActivity
from omnigent.stores.project_ticket_store import ProjectTicketStore
from omnigent.util.uuid import InvalidUuidError, uuid_to_bytes


# The label an agent adds when a person must act on a ticket.
NEEDS_HUMAN_LABEL = "needs-human"


def _has_needs_human(ticket: dict[str, Any]) -> bool:
    return any(label.get("key") == NEEDS_HUMAN_LABEL for label in ticket.get("labels") or [])


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


def _has_running_agents(
    session_ids: list[str], sessions: dict[str, SessionActivity], *, has_more: bool
) -> bool | None:
    unknown = has_more
    for session_id in session_ids:
        activity = sessions.get(session_id)
        if activity is None or activity.archived is None:
            unknown = True
            continue
        if activity.archived:
            continue
        # Use this linked session's own turn, without project or child rollups.
        status = _session_status_cache.get(session_id, activity.live_status)
        if status == "running":
            return True
        if status not in ("idle", "waiting", "failed"):
            unknown = True
    return None if unknown else False


def create_project_tickets_router(
    store: ProjectTicketStore,
    conversations: ConversationStore,
    *,
    auth_provider: AuthProvider | None = None,
    permissions: PermissionStore | None = None,
    agents: AgentStore | None = None,
    agent_cache: AgentCache | None = None,
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

    async def check_session(who: TicketActor, project_id: str, session_id: str) -> str:
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
        return session_id

    async def enrich(project_id: str, tickets: list[dict[str, Any]], who: TicketActor) -> None:
        if not tickets:
            return
        links = await asyncio.to_thread(
            store.activity_sessions,
            project_id,
            [ticket["id"] for ticket in tickets],
            user_id=who.user_id,
        )
        session_ids = sorted({sid for link in links.values() for sid in link.session_ids})
        if permissions is not None and session_ids:
            if who.user_id is None:
                session_ids = []
            else:
                grants = await asyncio.to_thread(
                    permissions.list_for_sessions, session_ids, user_id=who.user_id
                )
                public_grants = await asyncio.to_thread(
                    permissions.list_for_sessions, session_ids, user_id=RESERVED_USER_PUBLIC
                )
                is_admin = await asyncio.to_thread(permissions.is_admin, who.user_id)
                session_ids = [
                    sid
                    for sid in session_ids
                    if _permission_level_from_grants(
                        who.user_id, grants.get(sid, []) + public_grants.get(sid, []), is_admin
                    )
                    is not None
                ]
        sessions = await asyncio.to_thread(conversations.get_session_activity, session_ids)

        def identities() -> dict[str, tuple[str, str | None]]:
            rows = conversations.get_conversations(session_ids)
            project = uuid_to_bytes(project_id).hex()
            known_agents = (
                agents.get_many(list({row.agent_id for row in rows.values() if row.agent_id}))
                if agents is not None
                else {}
            )
            return {
                row.id: (
                    row.sub_agent_name or known_agents[row.agent_id].name,
                    _resolve_harness(row, agent_cache=agent_cache, agents_by_id=known_agents),
                )
                for row in rows.values()
                if row.project_id == project
                and row.agent_id is not None
                and row.agent_id in known_agents
            }

        metadata = await asyncio.to_thread(identities)
        # Seen state belongs to the person viewing the board, not to agents reading tickets.
        unseen = (
            await asyncio.to_thread(
                store.unseen_activity,
                project_id,
                [ticket["id"] for ticket in tickets],
                user_id=who.user_id,
            )
            if who.session_id is None
            else {}
        )
        for ticket in tickets:
            ticket.update(unseen.get(ticket["id"], {}))
            linked = links[ticket["id"]]
            ticket["has_running_agents"] = _has_running_agents(
                linked.session_ids, sessions, has_more=linked.has_more
            )
            distinct = dict.fromkeys(
                metadata[sid] for sid in linked.session_ids if sid in metadata
            )
            ticket["agents"] = [
                {"agent_name": name, "harness": harness} for name, harness in distinct
            ]
            ticket["agents_has_more"] = linked.has_more

    @router.get("")
    async def list_tickets(
        request: Request,
        project_id: str,
        stage_id: TicketStageId | None = None,
        after: str | None = None,
        limit: int = Query(50, ge=1, le=100),
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        page = await asyncio.to_thread(
            store.list,
            project_id,
            user_id=who.user_id,
            stage_id=stage_id,
            after=after,
            limit=limit,
        )
        if who.session_id is not None:
            page["agent_guidance"] = PROJECT_TICKET_WORK_INSTRUCTION
        await enrich(project_id, page["data"], who)
        return page

    @router.post("", status_code=201)
    async def create_ticket(
        request: Request, project_id: str, body: CreateTicket
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        with _storage_errors():
            ticket = await asyncio.to_thread(store.create, project_id, body, who)
        wake(request)
        return ticket

    @router.get("/stages")
    async def ticket_stages(
        request: Request, project_id: str, include_usage: bool = False
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        return await asyncio.to_thread(
            store.stages, project_id, user_id=who.user_id, include_usage=include_usage
        )

    @router.put("/stages")
    async def update_ticket_stages(
        request: Request, project_id: str, body: UpdateTicketStages
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        with _storage_errors():
            configuration = await asyncio.to_thread(store.update_stages, project_id, body, who)
        wake(request)
        return configuration

    @router.get("/summary")
    async def ticket_summary(request: Request, project_id: str) -> dict[str, Any]:
        who = await actor(request, project_id)
        return await asyncio.to_thread(store.summary, project_id, user_id=who.user_id)

    @router.get("/{ticket_id}")
    async def get_ticket(request: Request, project_id: str, ticket_id: str) -> dict[str, Any]:
        who = await actor(request, project_id)
        ticket = await asyncio.to_thread(store.get, project_id, ticket_id, user_id=who.user_id)
        if who.session_id is not None:
            ticket["agent_guidance"] = PROJECT_TICKET_WORK_INSTRUCTION
        await enrich(project_id, [ticket], who)
        return ticket

    @router.patch("/{ticket_id}")
    async def update_ticket(
        request: Request, project_id: str, ticket_id: str, body: UpdateTicket
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        raises_human_request = (
            who.session_id is not None
            and body.labels is not None
            and any(label.key == NEEDS_HUMAN_LABEL for label in body.labels)
        )
        if raises_human_request:
            before = await asyncio.to_thread(
                store.get, project_id, ticket_id, user_id=who.user_id
            )
            raises_human_request = not _has_needs_human(before)
        with _storage_errors():
            ticket = await asyncio.to_thread(store.update, project_id, ticket_id, body, who)
        if raises_human_request and who.session_id is not None:
            # The asking session's chat holds the question, so it belongs in the Inbox.
            session_live_state.raise_attention(who.session_id)
        wake(request)
        return ticket

    @router.put("/{ticket_id}/read-state")
    async def mark_seen(
        request: Request, project_id: str, ticket_id: str, body: MarkTicketSeen
    ) -> dict[str, Any]:
        who = await actor(request, project_id)
        return await asyncio.to_thread(
            store.mark_seen, project_id, ticket_id, body, user_id=who.user_id
        )

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
        with _storage_errors():
            result = await asyncio.to_thread(
                store.link_session,
                project_id,
                ticket_id,
                session_id,
                body.action,
                who,
                involvement=body.involvement,
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
