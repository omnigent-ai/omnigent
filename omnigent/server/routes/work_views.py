"""Personal saved views, independent tasks and their session associations."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from omnigent.entities import Conversation
from omnigent.entities.work_view import (
    LinkWorkSession,
    RecordId,
    SaveView,
    SaveWorkTask,
    ViewQuery,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_OWNER, LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.session_orchestration import require_named_source_private
from omnigent.stores.conversation_store import ConversationStore, runner_seen_is_fresh
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectStore
from omnigent.stores.work_view_store import WorkViewStore


def session_view_status(conversation: Conversation) -> str:
    """Project persisted runtime state without treating an idle session as completed work."""
    if conversation.archived:
        return "archived"
    if conversation.pending_elicitation_count:
        return "needs_input"
    if conversation.live_status == "failed":
        return "failed"
    if not runner_seen_is_fresh(conversation.runner_last_seen):
        return "offline"
    if conversation.background_work_running:
        return "running"
    return conversation.live_status or "idle"


def query_session_view(
    body: ViewQuery, user_id: str | None, conversations: ConversationStore, projects: ProjectStore
) -> dict[str, Any]:
    """Bound sparse-filter scans and return a continuation even for an empty page."""
    project_names = {project.id: project.name for project in projects.list(user_id=user_id)}
    data: list[dict[str, Any]] = []
    cursor = body.cursor
    scanned = 0
    more = True
    while more and len(data) < body.limit and scanned < 1000:
        page = conversations.list_conversations(
            limit=min(200, 1000 - scanned),
            after=cursor,
            accessible_by=user_id,
            has_agent_id=True,
            kind="default",
            search_query=body.filters.search or None,
            search_scope="title",
            sort_by=body.display.order_by,
            order=body.display.direction,
            include_archived=body.filters.include_archived,
        )
        more = page.has_more
        if not page.data:
            more = False
            break
        for index, conversation in enumerate(page.data):
            cursor = conversation.id
            scanned += 1
            status = session_view_status(conversation)
            filters = body.filters
            if filters.statuses and status not in filters.statuses:
                continue
            project_id = (
                conversation.project_id if conversation.project_id in project_names else None
            )
            if filters.project_ids or filters.no_project:
                if not (
                    project_id in filters.project_ids
                    or (filters.no_project and project_id is None)
                ):
                    continue
            if (
                filters.updated_after is not None
                and conversation.updated_at < filters.updated_after
            ):
                continue
            data.append(
                {
                    "id": conversation.id,
                    "title": conversation.title or "Untitled session",
                    "status": status,
                    "project_id": project_id,
                    "project_name": project_names.get(project_id) if project_id else None,
                    "created_at": conversation.created_at,
                    "updated_at": conversation.updated_at,
                }
            )
            if len(data) == body.limit:
                more = more or index + 1 < len(page.data)
                break
    return {"data": data, "has_more": more, "next_cursor": cursor if more else None}


def create_work_views_router(
    projects: ProjectStore,
    conversations: ConversationStore,
    permissions: PermissionStore | None,
    auth_provider: AuthProvider | None,
) -> APIRouter:
    router = APIRouter()
    store = WorkViewStore(projects.storage_location)

    async def writer(request: Request) -> str | None:
        user_id = require_user(request, auth_provider)
        await asyncio.to_thread(
            require_named_source_private, request, user_id, conversations, permissions
        )
        return user_id

    @router.get("/work-views")
    async def list_views(request: Request, after: RecordId | None = None) -> dict[str, Any]:
        return await asyncio.to_thread(
            store.list_views, require_user(request, auth_provider), after
        )

    @router.post("/work-views/query")
    async def query_view(request: Request, body: ViewQuery) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        if body.entity_type == "sessions":
            return await asyncio.to_thread(
                query_session_view, body, user_id, conversations, projects
            )
        return await asyncio.to_thread(store.query, body, user_id)

    @router.post("/work-views")
    async def create_view(request: Request, body: SaveView) -> dict[str, Any]:
        return await asyncio.to_thread(store.save_view, body, await writer(request))

    @router.get("/work-views/{view_id}")
    async def get_view(request: Request, view_id: RecordId) -> dict[str, Any]:
        return await asyncio.to_thread(
            store.get_view, view_id, require_user(request, auth_provider)
        )

    @router.put("/work-views/{view_id}")
    async def update_view(request: Request, view_id: RecordId, body: SaveView) -> dict[str, Any]:
        return await asyncio.to_thread(store.save_view, body, await writer(request), view_id)

    @router.delete("/work-views/{view_id}")
    async def delete_view(request: Request, view_id: RecordId) -> dict[str, bool]:
        await asyncio.to_thread(store.delete_view, view_id, await writer(request))
        return {"deleted": True}

    @router.post("/work-tasks")
    async def create_task(request: Request, body: SaveWorkTask) -> dict[str, Any]:
        return await asyncio.to_thread(store.save_task, body, await writer(request))

    @router.get("/work-tasks/{task_id}")
    async def get_task(request: Request, task_id: RecordId) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        task = await asyncio.to_thread(store.get_task, task_id, user_id)
        visible = []
        for link in task["sessions"]:
            try:
                await require_access(
                    user_id, link["session_id"], LEVEL_READ, permissions, conversations
                )
            except OmnigentError as exc:
                if exc.code in (ErrorCode.FORBIDDEN, ErrorCode.NOT_FOUND):
                    continue
                raise
            conversation = await asyncio.to_thread(
                conversations.get_conversation, link["session_id"]
            )
            if conversation is not None:
                visible.append(
                    {
                        **link,
                        "title": conversation.title or "Untitled session",
                        "status": session_view_status(conversation),
                    }
                )
        task["sessions"] = visible
        return task

    @router.put("/work-tasks/{task_id}")
    async def update_task(
        request: Request, task_id: RecordId, body: SaveWorkTask
    ) -> dict[str, Any]:
        return await asyncio.to_thread(store.save_task, body, await writer(request), task_id)

    @router.post("/work-tasks/{task_id}/sessions")
    async def link_session(
        request: Request, task_id: RecordId, body: LinkWorkSession
    ) -> dict[str, bool]:
        user_id = await writer(request)
        await require_access(user_id, body.session_id, LEVEL_OWNER, permissions, conversations)
        conversation = await asyncio.to_thread(conversations.get_conversation, body.session_id)
        if conversation is None:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        await asyncio.to_thread(store.link_session, task_id, body, user_id)
        return {"linked": True}

    @router.delete("/work-tasks/{task_id}/sessions/{session_id}")
    async def unlink_session(
        request: Request, task_id: RecordId, session_id: RecordId
    ) -> dict[str, bool]:
        await asyncio.to_thread(store.unlink_session, task_id, session_id, await writer(request))
        return {"linked": False}

    return router
