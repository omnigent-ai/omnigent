"""Catalog execution behind the existing session MCP authorization and policy path."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import Request

from omnigent.entities import Conversation
from omnigent.server.auth import RESERVED_USER_LOCAL, local_single_user_enabled
from omnigent.server.mcp_registry import McpRegistry, McpUpstreamError
from omnigent.server.routes.connections_base import ConnectionError
from omnigent.spec.types import AgentSpec, MCPServerConfig

_logger = logging.getLogger(__name__)


def registry_user(actor: dict[str, str] | None) -> str:
    user_id = (actor or {}).get("run_as")
    if user_id:
        return user_id
    if local_single_user_enabled():
        return RESERVED_USER_LOCAL
    raise ConnectionError("An authenticated user is required for registry MCP services")


async def registry_tools(
    registry: McpRegistry,
    request: Request,
    spec: AgentSpec,
    user_id: str | None,
) -> list[dict[str, Any]]:
    tools = []
    user = registry_user({"run_as": user_id} if user_id else None)
    for config in spec.mcp_servers:
        if config.transport != "registry":
            continue
        try:
            definitions = await registry.execute(config.name, user, request.app.state)
            for tool in definitions:
                if config.tools is None or tool.name in config.tools:
                    tools.append(
                        {
                            **tool.model_dump(by_alias=True, exclude_none=True),
                            "name": f"{config.name}__{tool.name}",
                        }
                    )
        except ConnectionError:
            continue
        except Exception:  # noqa: BLE001 — SDK errors may contain upstream credentials
            _logger.warning("Registry MCP discovery failed for service %s", config.name)
    return tools


async def execute_registry_tool(
    request: Request | None,
    conv: Conversation,
    config: MCPServerConfig,
    name: str,
    arguments: dict[str, Any],
    actor: dict[str, str] | None,
) -> dict[str, Any]:
    try:
        registry = getattr(request.app.state, "mcp_registry", None) if request else None
        if registry is None or request is None or conv.archived:
            raise ConnectionError("Registry MCP is unavailable for this session")
        tool = name[len(config.name) + 2 :]
        if config.tools is not None and tool not in config.tools:
            raise ConnectionError("Tool is not enabled for this session")
        result = await registry.execute(
            config.name, registry_user(actor), request.app.state, tool=tool, arguments=arguments
        )
        output = "\n".join(
            getattr(block, "text", None) or block.model_dump_json() for block in result.content
        )
        _logger.info(
            "Registry MCP call service=%s tool=%s session=%s error=%s",
            config.name,
            tool,
            conv.id,
            result.isError,
        )
        return {"result": {"output": output}, "isError": result.isError}
    except McpUpstreamError as exc:
        _logger.warning(
            "Registry MCP call failed service=%s tool=%s session=%s failure=%s http_status=%s",
            config.name,
            name,
            conv.id,
            exc.kind,
            exc.status_code,
        )
        return {"error": {"code": -32000, "message": str(exc)}}
    except ConnectionError as exc:
        return {"error": {"code": -32000, "message": str(exc)}}
    except Exception:  # noqa: BLE001 — do not expose upstream exception details
        _logger.warning("Registry MCP call failed service=%s session=%s", config.name, conv.id)
        return {
            "error": {
                "code": -32000,
                "message": (
                    "MCP request failed. Check the connection in Settings > MCP; "
                    "the operation was not retried."
                ),
            }
        }
