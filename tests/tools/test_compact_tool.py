"""Tests for the sys_compact builtin."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.spec.types import AgentSpec
from omnigent.tools import ToolManager
from omnigent.tools.builtins.compact import SysCompactTool
from omnigent.runner.tool_dispatch import _execute_session_control_tool, should_dispatch_locally


def test_sys_compact_is_registered_and_runner_dispatched() -> None:
    """ToolManager exposes sys_compact and the runner owns dispatch."""
    schemas = {s["function"]["name"]: s["function"] for s in ToolManager(AgentSpec(spec_version=1)).get_tool_schemas()}

    assert SysCompactTool.name() in schemas
    assert schemas[SysCompactTool.name()]["parameters"] == {
        "type": "object",
        "properties": {},
        "additionalProperties": False,
    }
    assert should_dispatch_locally(SysCompactTool.name()) is True


@pytest.mark.asyncio
async def test_sys_compact_posts_current_session_compact_event() -> None:
    """The runner wrapper reuses the existing session-events compact path."""
    seen: list[dict[str, Any]] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            {
                "method": request.method,
                "path": request.url.path,
                "body": json.loads(request.content.decode()),
            }
        )
        return httpx.Response(200, json={"queued": False})

    async with httpx.AsyncClient(
        base_url="http://testserver",
        transport=httpx.MockTransport(handler),
    ) as client:
        result = await _execute_session_control_tool(
            SysCompactTool.name(),
            server_client=client,
            conversation_id="conv_parent",
        )

    payload = json.loads(result)
    assert payload["status"] == "compact_requested"
    assert payload["session_id"] == "conv_parent"
    assert payload["handled_by_runner"] is True
    assert seen == [
        {
            "method": "POST",
            "path": "/v1/sessions/conv_parent/events",
            "body": {"type": "compact", "data": {}},
        }
    ]


@pytest.mark.asyncio
async def test_sys_compact_reports_missing_conversation_id() -> None:
    """The control wrapper fails closed when no current session is bound."""
    result = await _execute_session_control_tool(
        SysCompactTool.name(),
        server_client=httpx.AsyncClient(base_url="http://testserver"),
        conversation_id=None,
    )

    assert result == "Error: sys_compact requires conversation_id"
