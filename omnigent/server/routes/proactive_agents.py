"""Admin settings for proactive agents: ``GET``/``PUT /v1/admin/proactive-agents``.

The settings only turn agents on or off; the hidden tasks that run them are
created per user on session create and are never listed or edited here.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, ConfigDict

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.proactive_agent import (
    PROACTIVE_AGENT_NAME,
    ProactiveAgentSettings,
    ProactiveAgentState,
)
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.stores import PermissionStore

# Agents an admin can toggle, with the label the settings page shows.
_AGENTS = (
    {
        "name": PROACTIVE_AGENT_NAME,
        "label": "Inbox suggestions",
        "description": "Reads each user's recent sessions on weekday mornings and posts "
        "follow-ups, related information, and reminders to their Inbox.",
    },
)
_AGENT_NAMES = frozenset(agent["name"] for agent in _AGENTS)


class SetProactiveAgentsRequest(BaseModel):
    """Body for ``PUT /v1/admin/proactive-agents``; omitted fields keep their value."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    agents: dict[str, bool] | None = None


def create_proactive_agents_router(
    settings: ProactiveAgentSettings,
    *,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> APIRouter:
    """Build the admin proactive-agent settings router (mounted under ``/v1``).

    :param settings: Where the settings are stored.
    :param auth_provider: Auth provider; ``None`` when auth is off.
    :param permission_store: Permission store used for the admin check;
        ``None`` in single-user mode, where the local user manages settings.
    """
    router = APIRouter()

    async def _require_admin(request: Request) -> None:
        if permission_store is None:
            return
        user_id = get_user_id(request, auth_provider)
        if user_id is None:
            raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
        if not await asyncio.to_thread(permission_store.is_admin, user_id):
            raise OmnigentError(
                "Admin privileges required to manage proactive agents",
                code=ErrorCode.FORBIDDEN,
            )

    def _response(state: ProactiveAgentState) -> dict[str, Any]:
        return {
            "object": "proactive_agents",
            "enabled": state.enabled,
            "editable": settings.editable,
            "agents": [
                {**agent, "enabled": state.agent_enabled(agent["name"])} for agent in _AGENTS
            ],
        }

    @router.get("/admin/proactive-agents")
    async def get_proactive_agents(request: Request) -> dict[str, Any]:
        """Report whether proactive agents run and whether this server can change it."""
        await _require_admin(request)
        return _response(await asyncio.to_thread(settings.read))

    @router.put("/admin/proactive-agents")
    async def set_proactive_agents(
        request: Request, body: SetProactiveAgentsRequest
    ) -> dict[str, Any]:
        """Turn proactive agents on or off (admin only)."""
        await _require_admin(request)
        if not settings.editable:
            raise OmnigentError(
                "Proactive agents are managed by this deployment and cannot be changed here.",
                code=ErrorCode.FORBIDDEN,
            )
        unknown = sorted(set(body.agents or {}) - _AGENT_NAMES)
        if unknown:
            raise OmnigentError(
                f"Unknown proactive agent(s): {', '.join(unknown)}",
                code=ErrorCode.INVALID_INPUT,
            )
        current = await asyncio.to_thread(settings.read)
        updated = ProactiveAgentState(
            enabled=current.enabled if body.enabled is None else body.enabled,
            agents={**current.agents, **(body.agents or {})},
        )
        await asyncio.to_thread(settings.write, updated)
        return _response(updated)

    return router
