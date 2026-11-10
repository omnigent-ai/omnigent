"""On/off settings for the proactive agent: one for the workspace, one per user.

Every route returns 404 when the deployment does not offer the agent, so the
settings stay hidden there. The agent runs for a user only when the workspace
setting and the user's own setting are both on.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores import PermissionStore
from omnigent.stores.proactive_agent_settings_store import ProactiveAgentSettingsStore


class ProactiveAgentSettings(BaseModel):
    """The caller's view of the settings.

    :param workspace_enabled: The admin's workspace-wide setting.
    :param user_enabled: The caller's own setting.
    :param enabled: Whether the agent runs for the caller: both settings on.
    :param can_manage_workspace: Whether the caller may change the workspace setting.
    """

    workspace_enabled: bool
    user_enabled: bool
    enabled: bool
    can_manage_workspace: bool


class ProactiveAgentToggle(BaseModel):
    """Body of a settings change."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


def create_proactive_agent_settings_router(
    store: ProactiveAgentSettingsStore,
    *,
    offered: Callable[[], bool],
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the proactive agent settings router (mounted under ``/v1``).

    :param store: Where the settings are stored.
    :param offered: Whether the deployment offers the agent to this request's caller.
    :param auth_provider: Auth provider; ``None`` when auth is off.
    :param permission_store: Permission store; ``None`` in single-user mode,
        where the one user manages the workspace setting.
    """

    def require_offered() -> None:
        if not offered():
            raise HTTPException(status_code=404, detail="not found")

    router = APIRouter(dependencies=[Depends(require_offered)])

    def _caller(request: Request) -> str:
        user_id = require_user(request, auth_provider)
        return user_id if user_id is not None else RESERVED_USER_LOCAL

    async def _can_manage(user_id: str) -> bool:
        if permission_store is None:
            return True
        return await asyncio.to_thread(permission_store.is_admin, user_id)

    async def _settings(user_id: str) -> ProactiveAgentSettings:
        workspace_enabled, user_enabled = await asyncio.to_thread(store.read, user_id)
        return ProactiveAgentSettings(
            workspace_enabled=workspace_enabled,
            user_enabled=user_enabled,
            enabled=workspace_enabled and user_enabled,
            can_manage_workspace=await _can_manage(user_id),
        )

    @router.get("/proactive-agent/settings", response_model=ProactiveAgentSettings)
    async def read_settings(request: Request) -> ProactiveAgentSettings:
        return await _settings(_caller(request))

    @router.put("/proactive-agent/settings/me", response_model=ProactiveAgentSettings)
    async def set_user_setting(
        request: Request, body: ProactiveAgentToggle
    ) -> ProactiveAgentSettings:
        user_id = _caller(request)
        await asyncio.to_thread(store.set_user_enabled, user_id, body.enabled)
        return await _settings(user_id)

    @router.put("/proactive-agent/settings/workspace", response_model=ProactiveAgentSettings)
    async def set_workspace_setting(
        request: Request, body: ProactiveAgentToggle
    ) -> ProactiveAgentSettings:
        user_id = _caller(request)
        if not await _can_manage(user_id):
            raise OmnigentError(
                "Admin privileges required to change the workspace setting",
                code=ErrorCode.FORBIDDEN,
            )
        await asyncio.to_thread(store.set_workspace_enabled, body.enabled)
        return await _settings(user_id)

    return router
