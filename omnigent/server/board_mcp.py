"""The project board's tools as a server-run MCP service, for hosts that don't relay them.

A host built before the board tools existed doesn't offer ``sys_ticket_*`` or the
team tools to its sessions. Such a host does support registry MCP services, which
the server runs. So for a project session on a host that doesn't report
``project_tools``, the served spec selects :data:`BOARD_SERVICE_ID`, and its calls
run here through the same code and REST routes an up-to-date host uses.

Writes stay attributed as they are for an up-to-date host: each call names its
session as the orchestration source and forwards the caller's own runner token,
so the routes record the session only when that token binds to its runner.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from fastapi import Request

from omnigent.entities import Conversation
from omnigent.runner.identity import (
    ORCHESTRATION_SOURCE_HEADER,
    RUNNER_TUNNEL_TOKEN_HEADER,
    token_bound_runner_id,
)
from omnigent.spec.types import AgentSpec
from omnigent.tools.builtins.project_tickets import TICKET_TOOL_CLASSES

#: The reserved registry service name; it takes precedence over a catalog entry.
BOARD_SERVICE_ID = "omnigent_board"

# Tools that read or write the caller's local files can't run on the server.
_LOCAL_FILE_TOOLS = frozenset({"sys_project_artifact_save"})
BOARD_TOOLS = {
    tool.name(): tool for tool in TICKET_TOOL_CLASSES if tool.name() not in _LOCAL_FILE_TOOLS
}

# Headers that describe this request rather than who sent it.
_NOT_FORWARDED = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "accept-encoding",
        "expect",
        "x-omnigent-session-id",
        ORCHESTRATION_SOURCE_HEADER.lower(),
    }
)


def board_service_wanted(
    conversation: Conversation, spec: AgentSpec | None, host_capabilities: set[str] | None
) -> bool:
    """Whether a session's served spec should select the board service.

    :param host_capabilities: What the session's live host reports, or ``None``
        when it has no live host (an in-process runner relays the tools itself).
    """
    from omnigent.host.frames import CAP_PROJECT_TOOLS

    return (
        conversation.project_id is not None
        and not conversation.archived
        and spec is not None
        and spec.tool_groups.projects
        and host_capabilities is not None
        and CAP_PROJECT_TOOLS not in host_capabilities
    )


def bound_runner_token(request: Request, conversation: Conversation) -> str | None:
    """The caller's runner token when it binds to *conversation*'s runner."""
    token = (request.headers.get(RUNNER_TUNNEL_TOKEN_HEADER) or "").strip()
    if token and conversation.runner_id and token_bound_runner_id(token) == conversation.runner_id:
        return token
    return None


def board_tool_schemas() -> list[dict[str, Any]]:
    """The board tools in MCP ``tools/list`` shape."""
    tools = []
    for name, tool in BOARD_TOOLS.items():
        function = tool().get_schema()["function"]
        tools.append(
            {
                "name": name,
                "description": function["description"],
                "inputSchema": function["parameters"],
            }
        )
    return tools


async def execute_board_tool(
    request: Request, conversation: Conversation, name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    """Run one board tool as *conversation*, against this server's own API.

    :returns: ``{"result": {"output": ...}, "isError": ...}`` as registry tools do.
    """
    from omnigent.runner.project_tickets import execute_ticket_tool

    if name not in BOARD_TOOLS:
        return {"error": {"code": -32602, "message": f"Unknown board tool: {name!r}"}}
    if name == "sys_project_file_view" and arguments.get("save_path"):
        return {
            "error": {
                "code": -32602,
                "message": "save_path needs a host that relays the board tools; omit it",
            }
        }
    headers = {
        key: value for key, value in request.headers.items() if key.lower() not in _NOT_FORWARDED
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=request.app),
        base_url="http://omnigent.internal",
        headers=headers,
    ) as client:
        output = await execute_ticket_tool(name, arguments, client, conversation.id)
    return {"result": {"output": output}, "isError": _is_error(output)}


def _is_error(output: str) -> bool:
    try:
        parsed = json.loads(output)
    except ValueError:
        return False
    return isinstance(parsed, dict) and set(parsed) == {"error"}
