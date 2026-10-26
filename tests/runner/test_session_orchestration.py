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
        if request.url.path == "/v1/info":
            return httpx.Response(200, json={"request_managed_sandbox_host_id": True})
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
        assert len(body["host_id"]) == 32
        assert body["initial_input"]["data"]["content"][0]["text"] == "Start"
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
        if request.url.path == "/v1/info":
            return httpx.Response(200, json={})
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
@pytest.mark.parametrize(
    "failure", ["timeout", "non_json", "null", "list", 408, 429, 500, 502, 503, 504]
)
@pytest.mark.parametrize("after_routing", [False, True])
async def test_message_timeout_is_not_retried(failure, after_routing):
    requests = []

    def timeout(request):
        if request.method == "GET":
            return httpx.Response(200, json={"host_id": "worker"})
        requests.append(request)
        if after_routing and len(requests) == 1:
            return httpx.Response(400, json={"error": {"code": "wrong_replica"}})
        if failure == "non_json":
            return httpx.Response(202, text="Accepted")
        if failure in ("null", "list"):
            return httpx.Response(202, text="null" if failure == "null" else "[]")
        if isinstance(failure, int):
            return httpx.Response(failure)
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
    assert len(requests) == (2 if after_routing else 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [403, 502])
async def test_same_host_first_task_failure_returns_created_session(status):
    session_id = uuid4().hex
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/events"):
            return httpx.Response(status, text="First task unavailable")
        return httpx.Response(201, json={"id": session_id})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {"agent_id": "worker", "placement": "same_host", "message": "Start"}
                ),
                server_client=client,
                conversation_id=uuid4().hex,
            )
        )
    assert result["conversation_id"] == session_id
    assert result["status"] == "created_initial_message_failed"
    assert "Session created" in result["error"]
    assert "do not automatically resend" in result["error"]
    assert result["initial_message"]["error"]
    assert len(requests) == 2


@pytest.mark.asyncio
async def test_message_routing_accepts_null_snapshot_labels():
    requests = []

    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json={"labels": None})
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(400, json={"error": {"code": "wrong_replica"}})
        return httpx.Response(202, json={"queued": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_message",
                arguments=json.dumps({"session_id": uuid4().hex, "message": "Start"}),
                server_client=client,
                conversation_id=uuid4().hex,
            )
        )
    assert result == {"queued": True}
    assert len(requests) == 2


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


@pytest.mark.asyncio
@pytest.mark.parametrize("supported", [False, True])
async def test_managed_create_routes_to_reserved_host_through_auth_retry(monkeypatch, supported):
    from omnigent.cli_auth import OMNIGENT_SLICE_KEY_HEADER, open_server_client
    from omnigent.runner._entry import _RunnerDatabricksAuth

    monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
    monkeypatch.setenv("OMNIGENT_RUNNER_SLICE_KEY", "sender-host")
    requests = []

    def respond(request):
        if request.url.path == "/v1/info":
            return httpx.Response(200, json={"request_managed_sandbox_host_id": supported})
        requests.append((dict(request.headers), json.loads(request.content)))
        if len(requests) == 1:
            return httpx.Response(401)
        return httpx.Response(201, json={"id": uuid4().hex})

    async with open_server_client(
        "http://sharded",
        auth=_RunnerDatabricksAuth(lambda: "test-token", "http://sharded"),
        transport=httpx.MockTransport(respond),
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps({"agent_id": "worker", "placement": "managed"}),
                server_client=client,
                conversation_id=uuid4().hex,
            )
        )
        assert client.headers[OMNIGENT_SLICE_KEY_HEADER] == "sender-host"
    assert "error" not in result, result
    assert len(requests) == 2
    assert requests[0] == requests[1]
    headers, body = requests[-1]
    assert headers.get(OMNIGENT_SLICE_KEY_HEADER.lower()) == body.get("host_id")
    assert ("host_id" in body) is supported
