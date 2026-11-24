"""Attach newly created sessions to personal work tasks before execution."""

from __future__ import annotations

import asyncio

from fastapi import Request

from omnigent.entities.work_view import LinkWorkSession
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.session_orchestration import require_named_source_private
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.project_store import ProjectStore
from omnigent.stores.work_view_store import WorkViewStore


async def prepare_task_session(
    request: Request,
    task_id: str | None,
    *,
    user_id: str | None,
    projects: ProjectStore | None,
    conversations: ConversationStore,
    permissions: PermissionStore | None,
) -> WorkViewStore | None:
    """Authorize task membership before creating a session or allocating resources."""
    if task_id is None:
        return None
    if projects is None:
        raise OmnigentError("Tasks are unavailable on this server", code=ErrorCode.INVALID_INPUT)
    await asyncio.to_thread(
        require_named_source_private, request, user_id, conversations, permissions
    )
    store = WorkViewStore(projects.storage_location)
    await asyncio.to_thread(store.get_task, task_id, user_id)
    return store


async def link_created_task_session(
    store: WorkViewStore | None,
    task_id: str | None,
    session_id: str,
    *,
    user_id: str | None,
    conversations: ConversationStore,
) -> None:
    """Remove an unstarted session if its required task association cannot be saved."""
    if store is None or task_id is None:
        return
    try:
        await asyncio.to_thread(
            store.link_session, task_id, LinkWorkSession(session_id=session_id), user_id
        )
    except Exception:
        await conversations.delete_conversation(session_id)
        raise
