"""sys_session_create must wait out a busy server's create instead of stranding it."""

import json
from uuid import uuid4

import httpx
import pytest

from omnigent.runner.tool_dispatch import execute_tool

# A busy server was seen taking just over 30s to answer a managed create.
_SLOW_CREATE_SECONDS = 31.0


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["managed", "same_host"])
async def test_create_waits_for_a_server_slower_than_30s(placement):
    session_id = uuid4().hex
    creates = []

    def respond(request):
        if request.url.path == "/v1/info":
            return httpx.Response(200, json={"request_managed_sandbox_host_id": True})
        assert request.method == "POST" and request.url.path == "/v1/sessions/orchestrated"
        creates.append(request)
        # The mock answers instantly, so stand in for the slow server by failing
        # the way httpx would when the client stops reading before it answers.
        if request.extensions["timeout"]["read"] < _SLOW_CREATE_SECONDS:
            raise httpx.ReadTimeout("server still creating the session", request=request)
        return httpx.Response(201, json={"id": session_id, "agent_id": "worker"})

    arguments = {"agent_id": "worker", "placement": placement, "title": "Worker task"}
    if placement == "managed":
        arguments["message"] = "Start"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(arguments),
                server_client=client,
                conversation_id="manager",
            )
        )

    assert "error" not in result, result
    assert result["conversation_id"] == session_id
    assert len(creates) == 1
    # Only the read budget grows; connecting, writing and pool waits still fail fast.
    assert creates[0].extensions["timeout"] == {
        "connect": 30.0,
        "read": 120.0,
        "write": 30.0,
        "pool": 30.0,
    }
