"""Tool wire contracts, native discovery, and uncertain delivery handling."""

import asyncio
import json
from unittest.mock import Mock
from uuid import uuid4

import httpx
import pytest

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas, execute_tool
from omnigent.runtime.requester import (
    REQUESTER_HEADER,
    ExecutionRequester,
    InputRequester,
    ToolRequester,
)
from omnigent.spec.types import AgentSpec


@pytest.mark.parametrize("consumed", [False, True])
def test_requester_is_bound_to_execution(consumed):
    owner = InputRequester(user_id="owner")
    collaborator = InputRequester(user_id="collaborator")
    active = ExecutionRequester(owner)
    admitted = active.snapshot()
    active.begin_injection("steer", collaborator)
    assert active.snapshot().origin.user_id is None
    active.finish_injection("steer", consumed=consumed)
    assert active.snapshot().origin.user_id == (None if consumed else "owner")
    old_call = active.snapshot()
    fresh = ExecutionRequester(owner).snapshot()
    assert fresh.origin.user_id == "owner"
    assert fresh.execution_id != old_call.execution_id
    assert old_call.origin.user_id == (None if consumed else "owner")
    assert admitted.origin.user_id == "owner"
    if consumed:
        active.consume(owner)
        assert active.snapshot().origin.user_id is None


def test_requester_serialization_and_unknown_input():
    from omnigent.runtime.requester import REQUESTER_FIELD

    origin = InputRequester(user_id="owner", account_generation="generation")
    body = json.loads(json.dumps({REQUESTER_FIELD: origin.model_dump()}))
    assert InputRequester.from_body(body) == origin
    assert InputRequester.from_body({"created_by": "owner"}).user_id is None
    assert InputRequester.from_body({REQUESTER_FIELD: {"user_id": 123}}).user_id is None
    active = ExecutionRequester(origin)
    active.consume(InputRequester(user_id="owner", account_generation="replacement"))
    assert active.snapshot().origin.user_id is None


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
    requester = ExecutionRequester(InputRequester(user_id="owner")).snapshot()

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
                requester=requester,
            )
        )
    assert "error" not in result, result
    assert len(requests) == (2 if placement == "same_host" else 1)
    body = json.loads(requests[0].content)
    assert ToolRequester.model_validate_json(requests[0].headers[REQUESTER_HEADER]) == requester
    assert requests[0].url.path == "/v1/requester/sessions"
    if placement == "child":
        assert body["parent_session_id"] == "manager"
        assert ORCHESTRATION_SOURCE_HEADER not in requests[0].headers
        register.assert_called_once()
    else:
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
        assert requests[1].url.path == f"/v1/requester/sessions/{result['conversation_id']}/events"
        assert (
            ToolRequester.model_validate_json(requests[1].headers[REQUESTER_HEADER]) == requester
        )
        assert requests[1].headers[ORCHESTRATION_SOURCE_HEADER] == "manager"
        assert json.loads(requests[1].content)["data"]["message"] == "Start"
        assert result["initial_message"] == {"queued": True}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["child", "same_host", "managed", "message", "subagent"])
@pytest.mark.parametrize("status", [404, 405])
async def test_requester_mutations_do_not_fall_back_on_unsupported_server(operation, status):
    requests = []
    target = uuid4().hex
    requester = ExecutionRequester(InputRequester(user_id="collaborator")).snapshot()
    expected_path = (
        f"/v1/requester/sessions/{target}/events"
        if operation == "message"
        else "/v1/requester/sessions"
    )

    def unsupported(request):
        if request.url.path == "/v1/info":
            return httpx.Response(200, json={})
        if request.method == "GET" and request.url.path == "/v1/sessions/manager":
            return httpx.Response(200, json={"agent_id": "parent"})
        if request.method == "GET" and request.url.path == "/v1/sessions/manager/child_sessions":
            return httpx.Response(200, json={"data": []})
        requests.append(request)
        assert request.method == "POST"
        if request.url.path == expected_path:
            return httpx.Response(status, json={"detail": "Unsupported requester route"})
        # Older servers accept these mutations without preserving the requester.
        if request.url.path in {"/v1/sessions", "/v1/sessions/orchestrated"}:
            return httpx.Response(201, json={"id": target})
        if request.url.path == f"/v1/sessions/{target}/events":
            return httpx.Response(202, json={"queued": True})
        pytest.fail(f"Unexpected request: {request.method} {request.url.path}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unsupported), base_url="http://server"
    ) as client:
        output = await execute_tool(
            tool_name="sys_session_message"
            if operation == "message"
            else "sys_session_send"
            if operation == "subagent"
            else "sys_session_create",
            arguments=json.dumps(
                {"session_id": target, "message": "Start"}
                if operation == "message"
                else {"agent": "worker", "title": "probe", "args": "Start"}
                if operation == "subagent"
                else {"agent_id": "worker", "placement": operation, "message": "Start"}
            ),
            server_client=client,
            conversation_id="manager",
            requester=requester,
            session_inbox=asyncio.Queue(),
            agent_spec=AgentSpec(
                spec_version=1, spawn=True, sub_agents=[AgentSpec(spec_version=1, name="worker")]
            )
            if operation == "subagent"
            else None,
        )
    error = output if operation == "subagent" else json.loads(output)["error"]
    assert "unavailable on this server" in error
    assert "upgrade server and runner together" in error
    assert [r.url.path for r in requests] == [expected_path]
    if operation in ("subagent", "child"):
        assert json.loads(requests[0].content)["parent_session_id"] == "manager"
    else:
        assert requests[0].headers[ORCHESTRATION_SOURCE_HEADER] == "manager"
    assert ToolRequester.model_validate_json(requests[0].headers[REQUESTER_HEADER]) == requester


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure", ["timeout", "non_json", "null", "list", "empty", "non_bool", 408, 429, 503]
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
        if failure in ("empty", "non_bool"):
            return httpx.Response(202, json={} if failure == "empty" else {"queued": 1})
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
@pytest.mark.parametrize("status", [403, 502, "policy_denied"])
async def test_same_host_first_task_failure_returns_created_session(status):
    session_id = uuid4().hex
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/events"):
            if status == "policy_denied":
                return httpx.Response(
                    202, json={"queued": False, "denied": True, "reason": "Task needs approval"}
                )
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
    if status == "policy_denied":
        assert "Task needs approval" in result["error"]
        assert result["initial_message"]["denied"] is True
    assert len(requests) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "placement,reserves_host", [("same_host", False), ("managed", False), ("managed", True)]
)
@pytest.mark.parametrize(
    "failure",
    ["timeout", "non_json", "null", "list", "missing_id", "invalid_id", 408, 429, 503],
)
async def test_independent_creation_uncertain_result_is_not_retried(
    placement, reserves_host, failure
):
    requests = []

    def respond(request):
        if request.url.path == "/v1/info":
            return httpx.Response(200, json={"request_managed_sandbox_host_id": reserves_host})
        requests.append(request)
        assert request.url.path == "/v1/requester/sessions"
        if isinstance(failure, int):
            return httpx.Response(failure)
        if failure == "timeout":
            raise httpx.ReadTimeout("Response lost after creation")
        bodies = {
            "non_json": "Created",
            "null": "null",
            "list": "[]",
            "missing_id": "{}",
            "invalid_id": '{"id": 123}',
        }
        return httpx.Response(201, text=bodies[failure])

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {
                        "agent_id": "worker",
                        "placement": placement,
                        "message": "Start",
                        "title": "Review worker",
                    }
                ),
                server_client=client,
                conversation_id=uuid4().hex,
            )
        )
    assert "creation could not be confirmed" in result["error"]
    assert "inspect your sessions before creating another" in result["error"]
    assert "do not automatically retry" in result["error"]
    assert "sys_session_list" in result["error"]
    assert "sys_session_get_info" in result["error"]
    assert result["agent_id"] == "worker"
    assert result["title"] == "Review worker"
    if reserves_host:
        assert result["host_id"] == json.loads(requests[0].content)["host_id"]
    else:
        assert "host_id" not in result
    assert "conversation_id" not in result
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_managed_create_info_failure_does_not_attempt_creation():
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(503)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps({"agent_id": "worker", "placement": "managed"}),
                server_client=client,
                conversation_id=uuid4().hex,
            )
        )
    assert "sys_session_create failed" in result["error"]
    assert "could not be confirmed" not in result["error"]
    assert [r.url.path for r in requests] == ["/v1/info"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        "success",
        "lookup_http_error",
        "lookup_timeout",
        "lookup_invalid_json",
        "send_timeout",
        "hop_limit",
    ],
)
async def test_message_routing_lookup_and_keyless_delivery(outcome):
    requests = []
    lookups = []

    def respond(request):
        if request.method == "GET":
            lookups.append(request)
            if outcome == "lookup_http_error":
                return httpx.Response(503)
            if outcome == "lookup_timeout":
                raise httpx.ReadTimeout("Lookup failed")
            if outcome == "lookup_invalid_json":
                return httpx.Response(200, text="Not a snapshot")
            return httpx.Response(
                200,
                json={"labels": None, "parent_session_id": uuid4().hex}
                if outcome == "hop_limit"
                else {"labels": None},
            )
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(400, json={"error": {"code": "wrong_replica"}})
        if outcome in {"send_timeout", "hop_limit"}:
            raise httpx.ReadTimeout("Keyless delivery is uncertain")
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
    assert len(lookups) == (16 if outcome == "hop_limit" else 1)
    if outcome.startswith("lookup_"):
        assert result["error"] == "Message not delivered: target host could not be resolved"
        assert len(requests) == 1
    else:
        assert len(requests) == 2
        if outcome == "success":
            assert result == {"queued": True}
        else:
            assert "do not automatically resend" in result["error"]


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
            f"http://server/v1/requester/sessions/{destination.hex}/events"
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
