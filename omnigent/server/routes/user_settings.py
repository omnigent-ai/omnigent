"""Current-user settings API."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from pydantic import BaseModel

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.user_settings import background_session_titles_enabled_for_user
from omnigent.stores.permission_store import PermissionStore


class UserSettingsUpdate(BaseModel):
    """Writable current-user settings."""

    background_session_titles_enabled: bool


def create_user_settings_router(
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> APIRouter:
    """Build the current-user settings router mounted under ``/v1``."""
    router = APIRouter()

    def current_user_id(request: Request) -> str:
        user_id = require_user(request, auth_provider)
        if user_id is not None:
            return user_id
        if auth_provider is None and permission_store is not None:
            return RESERVED_USER_LOCAL
        raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)

    @router.get("/user-settings")
    async def get_user_settings(request: Request) -> dict[str, bool]:
        user_id = current_user_id(request)
        enabled = await background_session_titles_enabled_for_user(permission_store, user_id)
        return {"background_session_titles_enabled": enabled}

    @router.put("/user-settings")
    async def update_user_settings(
        request: Request,
        body: UserSettingsUpdate,
    ) -> dict[str, bool]:
        user_id = current_user_id(request)
        if permission_store is None:
            raise OmnigentError("User settings unavailable", code=ErrorCode.INTERNAL_ERROR)
        await asyncio.to_thread(
            permission_store.set_background_session_titles_enabled,
            user_id,
            body.background_session_titles_enabled,
        )
        return {"background_session_titles_enabled": body.background_session_titles_enabled}

    return router
