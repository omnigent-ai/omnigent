"""Runner dispatch and native-relay coverage for session renaming."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas, execute_tool
from omnigent.spec.types import AgentSpec


@pytest.mark.parametrize("spec", [AgentSpec(spec_version=1), None])
def test_native_relay_exposes_session_rename(spec: AgentSpec | None) -> None:
    schemas = build_native_relay_tool_schemas(spec)

    rename = next(schema for schema in schemas if schema["name"] == "sys_session_rename")

    assert rename["parameters"]["required"] == ["title"]
    assert rename["parameters"]["additionalProperties"] is False


@pytest.mark.asyncio
async def test_session_rename_dispatches_to_current_session() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"renamed": True, "title": "Debug auth timeout", "reason": None},
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://server",
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_session_rename",
            arguments=json.dumps({"title": "Debug auth timeout"}),
            server_client=server_client,
            conversation_id="conv_current",
            agent_spec=AgentSpec(spec_version=1),
        )

    assert json.loads(output) == {
        "renamed": True,
        "title": "Debug auth timeout",
        "reason": None,
    }
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url.path == "/v1/sessions/conv_current/auto-title"
    assert json.loads(requests[0].content) == {"title": "Debug auth timeout"}
