"""Managed creates we own must send a client-chosen host id when the server supports it."""

import json
import re
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from omnigent.runner.tool_dispatch import execute_tool

_HOST_ID = re.compile(r"[0-9a-f]{32}")
_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
@pytest.mark.parametrize("capability", [True, False])
async def test_runner_managed_create_sends_host_id_only_when_server_supports_it(capability):
    session_id = uuid4().hex
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/info":
            return httpx.Response(200, json={"request_managed_sandbox_host_id": capability})
        if request.url.path.endswith("/events"):
            return httpx.Response(202, json={"queued": True})
        if request.method == "GET":
            return httpx.Response(200, json={"id": session_id, "parent_session_id": "manager"})
        return httpx.Response(
            201, json={"id": session_id, "agent_id": "worker", "family": {"id": "manager"}}
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {
                        "agent_id": "worker",
                        "placement": "managed",
                        "message": "Start",
                        "title": "Worker task",
                    }
                ),
                server_client=client,
                conversation_id="manager",
            )
        )

    assert "error" not in result, result
    create = next(
        request
        for request in requests
        if request.method == "POST" and request.url.path == "/v1/sessions/orchestrated"
    )
    body = json.loads(create.content)
    assert body["host_type"] == "managed"
    if capability:
        assert _HOST_ID.fullmatch(body["host_id"]), body
    else:
        assert "host_id" not in body
