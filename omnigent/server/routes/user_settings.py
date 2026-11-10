"""Per-user settings the server applies on the caller's behalf (``/v1/settings``)."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.schemas import (
    SessionTitleInstructionsRequest,
    SessionTitleInstructionsResponse,
)
from omnigent.server.server_config import SESSION_TITLE_INSTRUCTIONS_MAX_CHARS
from omnigent.stores.user_settings_store import UserSettingsStore


def create_user_settings_router(
    store: UserSettingsStore,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the caller-scoped settings router.

    :param store: Where the settings persist.
    :param auth_provider: Identifies the caller; ``None`` is single-user mode.
    :returns: A configured :class:`APIRouter`.
    """
    router = APIRouter()

    @router.get(
        "/settings/session-title-instructions",
        response_model=SessionTitleInstructionsResponse,
    )
    async def get_session_title_instructions(
        request: Request,
    ) -> SessionTitleInstructionsResponse:
        """Read the caller's own title instructions."""
        user_id = require_user(request, auth_provider)
        instructions = await asyncio.to_thread(
            store.get_session_title_instructions, user_id=user_id
        )
        return SessionTitleInstructionsResponse(
            instructions=instructions, max_chars=SESSION_TITLE_INSTRUCTIONS_MAX_CHARS
        )

    @router.put(
        "/settings/session-title-instructions",
        response_model=SessionTitleInstructionsResponse,
    )
    async def save_session_title_instructions(
        request: Request, body: SessionTitleInstructionsRequest
    ) -> SessionTitleInstructionsResponse:
        """Save or clear the caller's title instructions."""
        user_id = require_user(request, auth_provider)
        try:
            instructions = await asyncio.to_thread(
                store.save_session_title_instructions, body.instructions, user_id=user_id
            )
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.INVALID_INPUT) from exc
        return SessionTitleInstructionsResponse(
            instructions=instructions, max_chars=SESSION_TITLE_INSTRUCTIONS_MAX_CHARS
        )

    return router
