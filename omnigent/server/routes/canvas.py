"""REST route for reading a conversation's canvas.

``GET /v1/canvas/{conversation_id}`` returns the canvas the agent authored via
``set_canvas`` (or 404 if none). Read-only; the agent is the only writer (via
the tool). Requires authentication in multi-user mode.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from omnigent.entities.canvas import Canvas
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.stores.canvas_store import CanvasStore
from omnigent.stores.permission_store import PermissionStore


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
) -> APIRouter:
    """Build the canvas router (mounted at ``/canvas/{conversation_id}``)."""
    router = APIRouter()

    @router.get("/canvas/{conversation_id}")
    async def get_canvas(request: Request, conversation_id: str) -> dict[str, Any]:
        """Return the conversation's canvas, or 404 if none is set."""
        user_id = get_user_id(request, auth_provider)
        if permission_store is not None and user_id is None:
            raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
        canvas = await asyncio.to_thread(store.get_by_conversation, conversation_id)
        if canvas is None:
            raise OmnigentError("No canvas for this conversation", code=ErrorCode.NOT_FOUND)
        return _to_response(canvas)

    return router
