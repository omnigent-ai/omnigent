"""Routes for thumbs up / thumbs down reactions on assistant responses."""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import attribution_user, get_user_id, require_access
from omnigent.server.routes._errors import session_not_found
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.reaction_store import ReactionStore

# Keep a thumbs-down note short: it is a hint for the author, not a review.
_NOTE_MAX_LEN = 1000


class SetReactionRequest(BaseModel):
    """Body for ``PUT /sessions/{id}/reactions/{response_id}``."""

    rating: Literal["up", "down"]
    note: str | None = Field(default=None, max_length=_NOTE_MAX_LEN)


def create_reactions_router(
    store: ReactionStore,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    conversation_store: ConversationStore | None = None,
) -> APIRouter:
    """Build the reactions router.

    Reacting needs only ``LEVEL_READ``: a reader rating a response is not
    editing the session. Each user's reaction is their own — setting one never
    touches another user's row.

    :param store: Reaction persistence.
    :param auth_provider: Resolves the request user; ``None`` in single-user mode.
    :param permission_store: Session access grants; ``None`` disables enforcement.
    :param conversation_store: Required with *permission_store*; also used to
        reject reactions on sessions that do not exist.
    :returns: A configured :class:`APIRouter`.
    :raises ValueError: If *permission_store* is given without *conversation_store*.
    """
    if permission_store is not None and conversation_store is None:
        raise ValueError("conversation_store is required when permission_store is provided")
    router = APIRouter()

    async def _require_session_access(user_id: str | None, session_id: str) -> Any:
        """Gate access and return the conversation (``None`` without a store)."""
        if permission_store is not None:
            assert conversation_store is not None
            await require_access(
                user_id, session_id, LEVEL_READ, permission_store, conversation_store
            )
        if conversation_store is None:
            return None
        conversation = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conversation is None:
            raise session_not_found()
        return conversation

    @router.get("/sessions/{session_id}/reactions")
    async def list_reactions(request: Request, session_id: str) -> list[dict[str, Any]]:
        """List the caller's reactions in a session.

        :param request: The incoming request, used to extract the user identity.
        :param session_id: The session, e.g. ``"conv_abc123"``.
        :returns: The caller's reactions, oldest first.
        """
        user_id = get_user_id(request, auth_provider)
        await _require_session_access(user_id, session_id)
        reactions = await asyncio.to_thread(
            store.list_for_user, session_id, attribution_user(user_id)
        )
        return [asdict(r) for r in reactions]

    @router.put("/sessions/{session_id}/reactions/{response_id}")
    async def set_reaction(
        request: Request, session_id: str, response_id: str, body: SetReactionRequest
    ) -> dict[str, Any]:
        """Create or replace the caller's reaction to a response.

        :param request: The incoming request, used to extract the user identity.
        :param session_id: The session, e.g. ``"conv_abc123"``.
        :param response_id: The assistant turn, e.g. ``"resp_abc"``.
        :param body: The rating and optional note.
        :returns: The persisted reaction.
        """
        user_id = get_user_id(request, auth_provider)
        conversation = await _require_session_access(user_id, session_id)
        note = body.note.strip() if body.note else None
        # Snapshot attribution from the session row, never from the client: a
        # later /model switch must not rewrite which model earned the rating.
        harness = getattr(conversation, "harness", None)
        model = getattr(conversation, "model_override", None)
        reaction = await asyncio.to_thread(
            store.set,
            session_id,
            response_id,
            body.rating,
            note or None,
            attribution_user(user_id),
            harness,
            model,
        )
        return asdict(reaction)

    @router.delete("/sessions/{session_id}/reactions/{response_id}")
    async def clear_reaction(
        request: Request, session_id: str, response_id: str
    ) -> dict[str, bool]:
        """Remove the caller's reaction to a response.

        :returns: ``{"removed": bool}`` — ``False`` when none existed.
        """
        user_id = get_user_id(request, auth_provider)
        await _require_session_access(user_id, session_id)
        removed = await asyncio.to_thread(
            store.clear, session_id, response_id, attribution_user(user_id)
        )
        return {"removed": removed}

    return router
