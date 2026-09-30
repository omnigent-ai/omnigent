"""Agent discovery distinguishes catalog membership from host readiness."""

import json

import httpx
import pytest

from omnigent.runner.tool_dispatch import execute_tool


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "report", [None, {}, {"codex-native": True, "jcode": False}, {"codex-native": True}]
)
async def test_agent_list_inherits_host_and_reports_availability(report: dict | None) -> None:
    async def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v1/agents":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "ag_jcode", "name": "jcode", "harness": "jcode"},
                        {"id": "ag_codex", "name": "codex", "harness": "codex-native"},
                    ]
                },
            )
        if path == "/v1/sessions":
            return httpx.Response(200, json={"data": []})
        if path == "/v1/sessions/child":
            return httpx.Response(200, json={"parent_session_id": "parent", "host_id": None})
        if path == "/v1/sessions/parent":
            return httpx.Response(200, json={"host_id": "host_test"})
        if path == "/v1/hosts/host_test":
            return httpx.Response(200, json={"configured_harnesses": report})
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handle), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_agent_list",
                arguments="{}",
                server_client=client,
                conversation_id="child",
            )
        )
    jcode, codex = result["builtins"]
    assert jcode["available_on_host"] is (False if report else None)
    assert jcode["unavailable_reason"] == ("unconfigured" if report else None)
    assert codex["available_on_host"] is (True if report else None)
