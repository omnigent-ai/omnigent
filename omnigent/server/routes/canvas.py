"""REST routes for a conversation's canvas.

``GET /v1/canvas/{conversation_id}`` returns the canvas the agent authored (or
404 if none). ``PUT /v1/canvas/{conversation_id}`` upserts it — used by the
runner's ``set_canvas`` tool proxy when the agent runs off-server (the runner
has no in-process CanvasStore, so it writes via this endpoint, mirroring how
the comment tools proxy over ``server_client``). Requires auth in multi-user
mode.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel

from omnigent.db.utils import generate_canvas_id
from omnigent.entities.canvas import (
    CANVAS_CONTENT_TYPES,
    MAX_CANVAS_CONTENT_BYTES,
    Canvas,
)
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime import get_caps
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access
from omnigent.stores.canvas_store import CanvasStore
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore


class UpsertCanvasBody(BaseModel):
    """Request body for ``PUT /canvas/{conversation_id}``."""

    title: str
    content: str
    content_type: str = "html"


def _to_response(canvas: Canvas) -> dict[str, Any]:
    """Serialize a :class:`Canvas` to a response dict."""
    return {
        "id": canvas.id,
        "object": "canvas",
        "conversation_id": canvas.conversation_id,
        "title": canvas.title,
        "content": canvas.content,
        "content_type": canvas.content_type,
        "created_at": canvas.created_at,
        "updated_at": canvas.updated_at,
    }


def create_canvas_router(
    store: CanvasStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    conversation_store: ConversationStore | None = None,
) -> APIRouter:
    """Build the canvas router (mounted at ``/canvas/{conversation_id}``).

    In multi-user mode (``permission_store`` set) the caller must have access
    to the target conversation — a canvas is conversation-scoped data, so
    reading or writing one requires the same permission as the conversation
    itself (read to GET, edit to PUT). ``require_access`` returns 404 (not 403)
    when the caller has no access at all, so it never leaks which conversation
    ids exist. Single-user deployments (no ``permission_store``) skip the check.
    """
    router = APIRouter()

    @router.get("/canvas/{conversation_id}")
    async def get_canvas(request: Request, conversation_id: str) -> dict[str, Any]:
        """Return the conversation's canvas, or 404 if none is set."""
        if not get_caps().canvas_enabled:
            raise OmnigentError("Canvas is not enabled on this server", code=ErrorCode.NOT_FOUND)
        user_id = get_user_id(request, auth_provider)
        await require_access(
            user_id, conversation_id, LEVEL_READ, permission_store, conversation_store
        )
        canvas = await asyncio.to_thread(store.get_by_conversation, conversation_id)
        if canvas is None:
            raise OmnigentError("No canvas for this conversation", code=ErrorCode.NOT_FOUND)
        return _to_response(canvas)

    @router.put("/canvas/{conversation_id}")
    async def upsert_canvas(
        request: Request, conversation_id: str, body: UpsertCanvasBody
    ) -> dict[str, Any]:
        """Create or overwrite the conversation's canvas (one per conversation)."""
        if not get_caps().canvas_enabled:
            raise OmnigentError("Canvas is not enabled on this server", code=ErrorCode.NOT_FOUND)
        user_id = get_user_id(request, auth_provider)
        await require_access(
            user_id, conversation_id, LEVEL_EDIT, permission_store, conversation_store
        )
        if body.content_type not in CANVAS_CONTENT_TYPES:
            raise OmnigentError(
                f"content_type must be one of {sorted(CANVAS_CONTENT_TYPES)}",
                code=ErrorCode.INVALID_INPUT,
            )
        if len(body.content.encode("utf-8")) > MAX_CANVAS_CONTENT_BYTES:
            raise OmnigentError(
                f"canvas content exceeds the {MAX_CANVAS_CONTENT_BYTES}-byte limit",
                code=ErrorCode.INVALID_INPUT,
            )
        canvas = await asyncio.to_thread(
            store.upsert,
            generate_canvas_id(),
            conversation_id,
            body.title,
            body.content,
            body.content_type,
        )
        return _to_response(canvas)

    return router
