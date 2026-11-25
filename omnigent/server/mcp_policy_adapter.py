"""Apply Omnigent session authorization and policies around any MCP backend."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request, Response

from omnigent.entities import Conversation
from omnigent.server.auth import LEVEL_EDIT, AuthProvider
from omnigent.server.board_mcp import (
    BOARD_SERVICE_ID,
    board_tool_schemas,
    bound_runner_token,
    execute_board_tool,
)
from omnigent.server.mcp_gateway import gateway_backend
from omnigent.server.mcp_identity import mcp_policy_actor
from omnigent.server.mcp_registry import McpRegistry, McpService
from omnigent.server.registry_gateway import execute_registry_tool, registry_user
from omnigent.server.routes._auth_helpers import require_access, require_user
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
    #: ``None`` for the server-run project board.
    service: McpService | None
    credential_user: str
    policy_actor: dict[str, str] | None


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
        if service_id == BOARD_SERVICE_ID:
            return self._authorize_board(request, conv, spec, caller)
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
            user = registry_user(_build_actor(caller))
            service = registry.service(service_id, user)
        except ConnectionError as exc:
            raise HTTPException(403, str(exc)) from None
        return McpPolicyContext(
            conv, config, service, user, mcp_policy_actor(request, conv, caller)
        )

    def _authorize_board(
        self, request: Request, conv: Conversation, spec: Any, caller: str | None
    ) -> McpPolicyContext:
        """Only the session's own runner may use the board, and only in a project."""
        if conv.project_id is None or spec is None or not spec.tool_groups.projects:
            raise HTTPException(403, "The project board isn't available to this session")
        if bound_runner_token(request, conv) is None:
            raise HTTPException(403, "The project board requires the session's bound runner")
        user = registry_user(_build_actor(caller))
        return McpPolicyContext(
            conv,
            MCPServerConfig(name=BOARD_SERVICE_ID, transport="registry"),
            None,
            user,
            mcp_policy_actor(request, conv, caller),
        )

    async def list_tools(
        self, request: Request, context: McpPolicyContext
    ) -> list[dict[str, Any]]:
        if context.service is None:
            return board_tool_schemas()
        backend = gateway_backend(request.app.state.mcp_registry, request.app.state)
        tools = await backend.list_tools(context.service, context.credential_user)
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
        service_id = context.service.id if context.service else BOARD_SERVICE_ID
        name = f"{service_id}__{params['name']}"

        async def execute(arguments: dict[str, Any]) -> dict[str, Any]:
            if context.service is None:
                return await execute_board_tool(
                    request, context.conversation, params["name"], arguments
                )
            return await execute_registry_tool(
                request,
                context.conversation,
                context.config,
                name,
                arguments,
                context.credential_user,
            )

        return await _handle_mcp_tools_call(
            rpc_id,
            context.conversation.id,
            {**params, "name": name},
            self.conversation_store,
            self.agent_store,
            None,
            actor=context.policy_actor,
            credential_user=context.credential_user,
            request=request,
            execute_tool=execute,
        )
