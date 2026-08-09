"""Framework checkpoint endpoints for session recovery."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.session_checkpoint import CHECKPOINT_KEY, SessionCheckpoint
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import (
    require_access as _require_access,
)
from omnigent.server.routes._auth_helpers import (
    require_user as _require_user,
)
from omnigent.server.schemas import (
    SessionCheckpointReplaceRequest,
    SessionCheckpointResponse,
)
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore


def register_checkpoint_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    """Register isolated framework checkpoint routes."""

    async def _authorized_session_state(
        request: Request,
        session_id: str,
        required_level: int,
    ) -> dict[str, Any]:
        user_id = _require_user(request, auth_provider)
        await _require_access(
            user_id,
            session_id,
            required_level,
            permission_store,
            conversation_store,
        )
        conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conversation is None:
            raise OmnigentError("Conversation not found", code=ErrorCode.NOT_FOUND)
        return dict(conversation.session_state)

    async def _load_authorized_checkpoint(
        request: Request,
        session_id: str,
        required_level: int,
    ) -> SessionCheckpoint | None:
        raw = (await _authorized_session_state(request, session_id, required_level)).get(
            CHECKPOINT_KEY
        )
        if raw is None:
            return None
        try:
            checkpoint = SessionCheckpoint.model_validate(raw)
        except (TypeError, ValueError) as exc:
            raise OmnigentError(
                "Stored framework checkpoint is invalid",
                code=ErrorCode.INTERNAL_ERROR,
            ) from exc
        if checkpoint.session_id != session_id:
            raise OmnigentError(
                "Stored framework checkpoint belongs to another session",
                code=ErrorCode.INTERNAL_ERROR,
            )
        return checkpoint

    @router.get(
        "/sessions/{session_id}/checkpoint",
        response_model=SessionCheckpointResponse,
    )
    async def get_session_checkpoint(
        request: Request,
        session_id: str,
    ) -> SessionCheckpointResponse:
        checkpoint = await _load_authorized_checkpoint(request, session_id, LEVEL_READ)
        return SessionCheckpointResponse(session_id=session_id, checkpoint=checkpoint)

    @router.put(
        "/sessions/{session_id}/checkpoint",
        response_model=SessionCheckpointResponse,
    )
    async def replace_session_checkpoint(
        request: Request,
        session_id: str,
        body: SessionCheckpointReplaceRequest,
    ) -> SessionCheckpointResponse:
        if body.checkpoint is not None and body.checkpoint.session_id != session_id:
            raise OmnigentError(
                "checkpoint session_id must match the route session_id",
                code=ErrorCode.INVALID_INPUT,
            )
        checkpoint = body.checkpoint
        await _authorized_session_state(request, session_id, LEVEL_EDIT)
        stored_checkpoint = checkpoint.model_dump(mode="json") if checkpoint is not None else None
        await asyncio.to_thread(
            conversation_store.set_session_state_key,
            session_id,
            CHECKPOINT_KEY,
            stored_checkpoint,
        )
        return SessionCheckpointResponse(session_id=session_id, checkpoint=checkpoint)
