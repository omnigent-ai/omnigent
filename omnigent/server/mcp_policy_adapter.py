"""Apply Omnigent session authorization and policies around any MCP backend."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, Response

from omnigent.entities import Conversation
from omnigent.server.auth import LEVEL_EDIT, AuthProvider
from omnigent.server.mcp_gateway import gateway_backend
from omnigent.server.mcp_registry import McpRegistry, McpService
from omnigent.server.registry_gateway import execute_registry_tool, registry_user
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._sessions.common import _TURN_ACTOR_LABEL
from omnigent.server.routes._sessions.helpers import _build_actor, _load_agent_spec_for_session
from omnigent.server.routes._sessions.orchestration import _handle_mcp_tools_call
from omnigent.server.routes.connections_base import ConnectionError
from omnigent.spec.types import MCPServerConfig
from omnigent.stores import AgentStore, ConversationStore
from omnigent.stores.permission_store import PermissionStore


@dataclass(frozen=True)
class McpPolicyContext:
    conversation: Conversation
    config: MCPServerConfig
    service: McpService
    user_id: str


class McpPolicyAdapter:
    """Keep session state and approval handling outside the execution backend."""

    def __init__(
        self,
        conversation_store: ConversationStore,
        agent_store: AgentStore,
        auth_provider: AuthProvider | None,
        permission_store: PermissionStore | None,
    ) -> None:
        self.conversation_store = conversation_store
        self.agent_store = agent_store
        self.auth_provider = auth_provider
        self.permission_store = permission_store

    async def authorize(
        self, request: Request, session_id: str, service_id: str
    ) -> McpPolicyContext:
        caller = require_user(request, self.auth_provider)
        await require_access(
            caller, session_id, LEVEL_EDIT, self.permission_store, self.conversation_store
        )
        conv = await asyncio.to_thread(self.conversation_store.get_conversation, session_id)
        if conv is None or conv.archived:
            raise HTTPException(404, "MCP session is unavailable")
        spec = await asyncio.to_thread(_load_agent_spec_for_session, conv, self.agent_store)
        config = (
            next(
                (
                    c
                    for c in spec.mcp_servers
                    if c.transport == "registry" and c.name == service_id
                ),
                None,
            )
            if spec
            else None
        )
        if config is None:
            raise HTTPException(403, "MCP service is not selected for this session")
        registry: McpRegistry | None = getattr(request.app.state, "mcp_registry", None)
        if registry is None:
            raise HTTPException(404, "MCP gateway is not configured")
        try:
            user = registry_user(_build_actor(conv.labels.get(_TURN_ACTOR_LABEL) or caller))
            service = registry.service(service_id, user)
        except ConnectionError as exc:
            raise HTTPException(403, str(exc)) from None
        return McpPolicyContext(conv, config, service, user)

    async def list_tools(
        self, request: Request, context: McpPolicyContext
    ) -> list[dict[str, Any]]:
        backend = gateway_backend(request.app.state.mcp_registry, request.app.state)
        tools = await backend.list_tools(context.service, context.user_id)
        return [
            t.model_dump(by_alias=True, exclude_none=True)
            for t in tools
            if t.name in context.service.tools
            and (context.config.tools is None or t.name in context.config.tools)
        ]

    async def call_tool(
        self,
        request: Request,
        context: McpPolicyContext,
        rpc_id: int | str | None,
        params: dict[str, Any],
    ) -> Response:
        name = f"{context.service.id}__{params['name']}"
        actor = {"run_as": context.user_id}

        async def execute(arguments: dict[str, Any]) -> dict[str, Any]:
            return await execute_registry_tool(
                request, context.conversation, context.config, name, arguments, actor
            )

        return await _handle_mcp_tools_call(
            rpc_id,
            context.conversation.id,
            {**params, "name": name},
            self.conversation_store,
            self.agent_store,
            None,
            actor=actor,
            request=request,
            execute_tool=execute,
        )
