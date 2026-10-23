"""Tool wire contracts, native discovery, and uncertain delivery handling."""

import json
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas, execute_tool
from omnigent.spec.types import AgentSpec


@pytest.mark.parametrize("spec", [AgentSpec(spec_version=1), None])
def test_native_message_schema(spec):
    schemas = build_native_relay_tool_schemas(spec)
    schema = next(s for s in schemas if s["name"] == "sys_session_message")
    assert schema["parameters"]["required"] == ["session_id", "message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["child", "same_host", "managed"])
async def test_create_placement(placement, monkeypatch):
    from omnigent.runner import subagent_work

    register = Mock()
    monkeypatch.setattr(subagent_work, "register_child_session", register)
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path.endswith("/events"):
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(201, json={"id": uuid4().hex, "agent_id": "worker"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {
                        "agent_id": "worker",
                        "placement": placement,
                        "message": "Start",
                        "title": "Worker task",
                    }
                ),
                server_client=client,
                conversation_id="manager",
            )
        )
    assert "error" not in result, result
    assert len(requests) == (2 if placement == "same_host" else 1)
    body = json.loads(requests[0].content)
    if placement == "child":
        assert requests[0].url.path == "/v1/sessions"
        assert body["parent_session_id"] == "manager"
        register.assert_called_once()
    else:
        assert requests[0].url.path == "/v1/sessions/orchestrated"
        assert "parent_session_id" not in body
        assert requests[0].headers[ORCHESTRATION_SOURCE_HEADER] == "manager"
        assert result["agent_id"] == "worker"
        assert result["title"] == "Worker task"
        register.assert_not_called()
    if placement == "managed":
        assert "sandbox_provider" not in body
        assert body["host_type"] == "managed"
        assert body["initial_input"]["data"]["content"][0]["text"] == (
            "[Message from session manager]\nStart"
        )
        assert "initial_items" not in body
    elif placement == "child":
        assert body["initial_items"][0]["data"]["content"][0]["text"] == "Start"
    else:
        assert "initial_items" not in body
        assert json.loads(requests[1].content)["data"]["message"] == "Start"
        assert result["initial_message"] == {"queued": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["same_host", "managed"])
@pytest.mark.parametrize("status", [404, 405])
async def test_independent_creation_does_not_fall_back_on_unsupported_server(placement, status):
    requests = []

    def unsupported(request):
        requests.append(request)
        return httpx.Response(status, json={"detail": "Unsupported route"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unsupported), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps({"agent_id": "worker", "placement": placement}),
                server_client=client,
                conversation_id="manager",
            )
        )
    assert "unavailable on this server" in result["error"]
    assert [r.url.path for r in requests] == ["/v1/sessions/orchestrated"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "non_json"])
async def test_message_timeout_is_not_retried(failure):
    requests = []

    def timeout(request):
        requests.append(request)
        if failure == "non_json":
            return httpx.Response(202, text="Accepted")
        raise httpx.ReadTimeout("uncertain")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(timeout), base_url="http://server"
    ) as client:
        result = await execute_tool(
            tool_name="sys_session_message",
            arguments=json.dumps({"session_id": uuid4().hex, "message": "Work"}),
            server_client=client,
            conversation_id="manager",
        )
    assert "do not automatically resend" in result
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("spelling", ["hex", "dashed", "legacy", "invalid", "query", "fragment"])
async def test_message_destination_is_a_uuid(spelling):
    destination = uuid4()
    supplied = {
        "hex": destination.hex,
        "dashed": str(destination),
        "legacy": f"conv_{destination.hex}",
        "invalid": "not-a-session-id",
        "query": f"{destination.hex}?unexpected",
        "fragment": f"{destination.hex}#unexpected",
    }[spelling]
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(202, json={"queued": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_message",
                arguments=json.dumps({"session_id": supplied, "message": "Work"}),
                server_client=client,
                conversation_id="manager",
            )
        )
    if spelling in ("hex", "dashed", "legacy"):
        assert result == {"queued": True}
        assert [str(r.url) for r in requests] == [
            f"http://server/v1/sessions/{destination.hex}/events"
        ]
    else:
        assert "error" in result
        assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["same_host", "managed"])
@pytest.mark.parametrize("message", [" ", "a" * 8001, 123], ids=["blank", "oversize", "non_text"])
async def test_independent_initial_message_is_validated_before_creation(placement, message):
    def unexpected(request):
        pytest.fail("Invalid first task must not create a worker")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unexpected), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {"agent_id": "worker", "placement": placement, "message": message}
                ),
                server_client=client,
                conversation_id="manager",
            )
        )
    assert "initial message of 1–8000 characters" in result["error"]
