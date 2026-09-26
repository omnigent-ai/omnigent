"""Session-independent execution contract for managed MCP gateways."""

from __future__ import annotations

from typing import Any, Protocol

from mcp.types import CallToolResult, Tool

from omnigent.server.mcp_registry import McpRegistry, McpService


class McpGatewayBackend(Protocol):
    """Execute approved operations; session authorization belongs to the adapter."""

    async def list_tools(self, service: McpService, user_id: str) -> list[Tool]:
        """Discover tools using the authenticated caller's account."""

    async def call_tool(
        self, service: McpService, user_id: str, tool: str, arguments: dict[str, Any]
    ) -> CallToolResult:
        """Execute a policy-approved call using the authenticated caller's account."""


class RegistryMcpBackend:
    """Use existing credentials and refresh for an HTTP MCP server or gateway."""

    def __init__(self, registry: McpRegistry, app_state: Any) -> None:
        self.registry = registry
        self.app_state = app_state

    async def list_tools(self, service: McpService, user_id: str) -> list[Tool]:
        return await self.registry.execute(service.id, user_id, self.app_state)

    async def call_tool(
        self, service: McpService, user_id: str, tool: str, arguments: dict[str, Any]
    ) -> CallToolResult:
        return await self.registry.execute(
            service.id, user_id, self.app_state, tool=tool, arguments=arguments
        )


def gateway_backend(registry: McpRegistry, app_state: Any) -> McpGatewayBackend:
    return getattr(app_state, "mcp_gateway_backend", None) or RegistryMcpBackend(
        registry, app_state
    )
