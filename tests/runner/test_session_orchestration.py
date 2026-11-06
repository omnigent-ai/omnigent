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


def test_create_schema_exposes_project_name():
    schemas = build_native_relay_tool_schemas(AgentSpec(spec_version=1, spawn=True))
    schema = next(s for s in schemas if s["name"] == "sys_session_create")
    # The schema forbids unlisted properties, so the project argument must be declared.
    assert schema["parameters"]["additionalProperties"] is False
    assert schema["parameters"]["properties"]["project_name"]["type"] == "string"
    assert "project_id" not in schema["parameters"]["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("placement", "profile", "capability", "numeric_id"),
    [
        *[
            (placement, profile, "enabled", False)
            for placement in ("child", "same_host", "managed")
            for profile in (
                None,
                {
                    "sandbox_provider": "company",
                    "sandbox_profile_id": "build",
                    "sandbox_profile_revision": "v1",
                },
                {"sandbox_profile_revision": "v1"},
                {"sandbox_provider": ""},
                {"sandbox_profile_id": " "},
                {"sandbox_profile_revision": ""},
            )
        ],
        *[
            ("managed", {"sandbox_profile_id": "build"}, capability, False)
            for capability in (
                "enabled",
                "missing",
                "disabled",
                "malformed",
                "non_bool",
                "timeout",
                "unavailable",
                "invalid_json",
            )
        ],
        *[
            ("managed", {"sandbox_provider": "company"}, capability, False)
            for capability in ("missing", "disabled")
        ],
        pytest.param("same_host", None, "enabled", True, id="same-host-managed-id"),
    ],
)
async def test_create_placement(placement, monkeypatch, profile, capability, numeric_id):
    from omnigent.runner import subagent_work

    session_id = "1234567890123456" if numeric_id else uuid4().hex
    register = Mock()
    monkeypatch.setattr(subagent_work, "register_child_session", register)
    requests = []

    def handler(request):
        requests.append(request)
        if request.url.path == "/v1/info":
            if capability == "timeout":
                raise httpx.ReadTimeout("Capability lookup timed out")
            if capability == "unavailable":
                return httpx.Response(503)
            if capability == "invalid_json":
                return httpx.Response(200, text="Unavailable")
            info = {"request_managed_sandbox_host_id": True}
            if capability != "missing":
                info["features"] = {
                    "enabled": {"sandbox_profiles": True},
                    "disabled": {"sandbox_profiles": False},
                    "malformed": [],
                    "non_bool": {"sandbox_profiles": "true"},
                }[capability]
            return httpx.Response(200, json=info)
        if request.url.path.endswith("/events"):
            return httpx.Response(202, json={"queued": True})
        return httpx.Response(201, json={"id": session_id, "agent_id": "worker"})

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
                        **(profile or {}),
                    }
                ),
                server_client=client,
                conversation_id="manager",
            )
        )
    if profile and placement != "managed":
        assert "requires placement='managed'" in result["error"]
        assert requests == []
        return
    if profile and any(not value.strip() for value in profile.values()):
        assert "nonempty strings" in result["error"]
        assert requests == []
        return
    if profile and "sandbox_profile_revision" in profile and "sandbox_profile_id" not in profile:
        assert "requires sandbox_profile_id" in result["error"]
        assert requests == []
        return
    if profile and profile.get("sandbox_profile_id") and capability != "enabled":
        assert "error" in result, result
        assert [(request.method, request.url.path) for request in requests] == [
            ("GET", "/v1/info")
        ]
        register.assert_not_called()
        return
    assert "error" not in result, result
    requests = [request for request in requests if request.url.path != "/v1/info"]
    assert result["conversation_id"] == session_id
    assert len(requests) == (2 if placement == "same_host" else 1)
    body = json.loads(requests[0].content)
    assert not {"project_id", "workspace", "git"} & body.keys()
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
        assert result["project_id"] is None
        assert result["project_name"] is None
        register.assert_not_called()
    if placement == "managed":
        if profile:
            assert all(body[key] == value for key, value in profile.items())
        else:
            assert "sandbox_provider" not in body
        assert body["host_type"] == "managed"
        assert len(body["host_id"]) == 32
        assert body["initial_input"]["data"]["content"][0]["text"] == "Start"
        assert "initial_items" not in body
    elif placement == "child":
        assert body["initial_items"][0]["data"]["content"][0]["text"] == "Start"
    else:
        assert "initial_items" not in body
        assert requests[1].url.path == f"/v1/sessions/{session_id}/events"
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
        assert request.url.path == "/v1/sessions/orchestrated"
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
@pytest.mark.parametrize(
    "spelling",
    [
        "hex",
        "dashed",
        "legacy",
        "managed",
        "invalid",
        "query",
        "fragment",
        "unicode",
        "non_text",
        "empty",
        "path",
        "managed_query",
        "padded_managed",
        "whitespace",
    ],
)
async def test_message_destination_accepts_supported_session_ids(spelling):
    destination = uuid4()
    supplied = {
        "hex": destination.hex,
        "dashed": str(destination),
        "legacy": f"conv_{destination.hex}",
        "managed": "1234567890123456",
        "invalid": "not-a-session-id",
        "query": f"{destination.hex}?unexpected",
        "fragment": f"{destination.hex}#unexpected",
        "unicode": "１２３４",
        "non_text": 1234567890123456,
        "empty": "",
        "path": "1234567890123456/events",
        "managed_query": "1234567890123456?unexpected",
        "padded_managed": "1234567890123456" + " " * 16,
        "whitespace": " " * 32,
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
    if spelling in ("hex", "dashed", "legacy", "managed"):
        assert result == {"queued": True}
        expected_id = supplied if spelling == "managed" else destination.hex
        assert [str(r.url) for r in requests] == [
            f"http://server/v1/sessions/{expected_id}/events"
        ]
    else:
        assert result == {"error": "Provide a valid session_id (UUID or numeric managed ID)"}
        assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "message",
    [None, 123, "", " ", "a" * 8001],
    ids=["missing", "non_text", "empty", "blank", "oversize"],
)
async def test_message_content_validation_is_distinct_from_invalid_id(message):
    def unexpected(request):
        pytest.fail("Invalid message must not send an HTTP request")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unexpected), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_message",
                arguments=json.dumps({"session_id": "1234567890123456", "message": message}),
                server_client=client,
                conversation_id="manager",
            )
        )
    assert result == {"error": "Provide a non-blank message of 1–8000 characters"}


@pytest.mark.asyncio
@pytest.mark.parametrize("target_kind", ["root", "child", "side_chat"])
async def test_managed_message_resolves_host_after_wrong_replica(target_kind):
    from omnigent.runner.session_requests import REQUEST_HOST_EXTENSION

    destination = "1234567890123456"
    parent = "1234567890123457"
    requests = []

    def respond(request):
        requests.append(request)
        if request.method == "GET":
            if request.url.path.endswith(parent) or target_kind == "root":
                return httpx.Response(200, json={"host_id": "worker", "runner_id": "runner"})
            snapshot = {"runner_id": "runner", "parent_session_id": parent}
            if target_kind == "side_chat":
                snapshot = {
                    "runner_id": "runner",
                    "labels": {"omnigent.side_chat": "1", "omnigent.side_chat.source_id": parent},
                }
            return httpx.Response(200, json=snapshot)
        if len(requests) == 1:
            return httpx.Response(400, json={"error": {"code": "wrong_replica"}})
        assert request.extensions[REQUEST_HOST_EXTENSION] == "worker"
        return httpx.Response(202, json={"queued": True})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_message",
                arguments=json.dumps({"session_id": destination, "message": "Work"}),
                server_client=client,
                conversation_id=parent,
            )
        )
    assert result == {"queued": True}
    lookup_ids = [destination] if target_kind == "root" else [destination, parent]
    assert [(r.method, r.url.path) for r in requests] == [
        ("POST", f"/v1/sessions/{destination}/events"),
        *[("GET", f"/v1/sessions/{session_id}") for session_id in lookup_ids],
        ("POST", f"/v1/sessions/{destination}/events"),
    ]
    for request in (requests[0], requests[-1]):
        assert request.headers[ORCHESTRATION_SOURCE_HEADER] == parent
        assert json.loads(request.content) == {
            "type": "session_message",
            "data": {"message": "Work"},
        }


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


_PROJECT_ID = "ab" * 16


@pytest.mark.asyncio
@pytest.mark.parametrize("placement", ["same_host", "managed"])
@pytest.mark.parametrize("named_by", ["existing_name", "new_name"])
async def test_independent_create_files_a_named_project(placement, named_by):
    session_id = uuid4().hex
    requests = []

    def respond(request):
        requests.append(request)
        path = request.url.path
        if path == "/v1/info":
            return httpx.Response(200, json={})
        if path == f"/v1/projects/{_PROJECT_ID}":
            return httpx.Response(200, json={"id": _PROJECT_ID, "name": "Triage"})
        if path == "/v1/projects" and request.method == "GET":
            found = [{"id": _PROJECT_ID, "name": "Triage"}] if named_by == "existing_name" else []
            return httpx.Response(200, json={"data": [{"id": "other", "name": "Other"}, *found]})
        if path == "/v1/projects":
            return httpx.Response(200, json={"id": _PROJECT_ID, "name": "Triage"})
        project_id = json.loads(request.content).get("project_id")
        return httpx.Response(201, json={"id": session_id, "project_id": project_id})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {"agent_id": "worker", "placement": placement, "project_name": "Triage"}
                ),
                server_client=client,
                conversation_id="manager",
            )
        )
    assert "error" not in result, result
    lookup = {
        "existing_name": [("GET", "/v1/projects")],
        "new_name": [("GET", "/v1/projects"), ("POST", "/v1/projects")],
    }[named_by]
    calls = [(r.method, r.url.path) for r in requests if r.url.path != "/v1/info"]
    # The handle's project name is read by id after the create.
    name_read = ("GET", f"/v1/projects/{_PROJECT_ID}")
    assert calls == [*lookup, ("POST", "/v1/sessions/orchestrated"), name_read]
    if placement == "managed":
        # The project is resolved after the managed capability read.
        assert requests[0].url.path == "/v1/info"
    create = next(r for r in requests if r.url.path == "/v1/sessions/orchestrated")
    body = json.loads(create.content)
    assert body["agent_id"] == "worker"
    assert body["project_id"] == _PROJECT_ID
    # Explicit nulls stop the named project's defaults from changing the placement.
    assert "workspace" in body and body["workspace"] is None
    assert "git" in body and body["git"] is None
    assert result["project_id"] == _PROJECT_ID
    assert result["project_name"] == "Triage"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("arguments", "error"),
    [
        (
            {"placement": "same_host", "project_id": _PROJECT_ID},
            "takes project_name, the project's name as shown in the UI, not project_id",
        ),
        ({"project_name": "T"}, "project_name requires placement='same_host' or 'managed'"),
        (
            {"placement": "child", "project_name": "T"},
            "project_name requires placement='same_host' or 'managed'",
        ),
        ({"placement": "managed", "project_name": ""}, "nonempty string"),
        ({"placement": "managed", "project_name": 123}, "nonempty string"),
        ({"placement": "managed", "project_name": "  "}, "nonempty string"),
        (
            {"agent_id": None, "config_path": "agent.yaml", "project_name": "T"},
            "project_name requires placement='same_host' or 'managed'",
        ),
    ],
)
async def test_project_arguments_are_refused_before_any_request(arguments, error):
    requests = []

    def unexpected(request):
        requests.append(request)
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(unexpected), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps({"agent_id": "worker", **arguments}),
                server_client=client,
                conversation_id="manager",
            )
        )
    assert error in result.get("error", ""), result
    assert requests == []


@pytest.mark.asyncio
async def test_a_refused_managed_create_resolves_no_project():
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/v1/info":
            return httpx.Response(200, json={"features": {"sandbox_profiles": False}})
        return httpx.Response(500)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {
                        "agent_id": "worker",
                        "placement": "managed",
                        "sandbox_profile_id": "build",
                        "project_name": "New project",
                    }
                ),
                server_client=client,
                conversation_id="manager",
            )
        )
    assert "sandbox profile support" in result["error"]
    # The project is resolved last, so this refusal creates no project.
    assert [r.url.path for r in requests] == ["/v1/info"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "arguments",
    [
        {"placement": "same_host", "project_id": None, "project_name": None},
        {"project_name": None},
        {"placement": "managed", "project_name": None},
    ],
)
async def test_null_project_arguments_count_as_absent(arguments):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path == "/v1/info":
            return httpx.Response(200, json={})
        if request.url.path == "/v1/projects/inherited":
            return httpx.Response(200, json={"id": "inherited", "name": "Creator's project"})
        return httpx.Response(201, json={"id": uuid4().hex, "project_id": "inherited"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps({"agent_id": "worker", **arguments}),
                server_client=client,
                conversation_id="manager",
            )
        )
    assert "error" not in result, result
    creates = [r for r in requests if r.url.path in ("/v1/sessions", "/v1/sessions/orchestrated")]
    assert len(creates) == 1
    assert not {"project_id", "workspace", "git"} & json.loads(creates[0].content).keys()
    if arguments.get("placement") in ("same_host", "managed"):
        # The handle reports the project the server filed the session in, here the creator's.
        assert result["project_id"] == "inherited"
        assert result["project_name"] == "Creator's project"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "error"),
    [
        (500, "Could not resolve project_name"),
        ("connect_error", "Could not resolve project_name"),
    ],
)
async def test_a_failed_project_lookup_creates_no_session(status, error):
    requests = []

    def respond(request):
        requests.append(request)
        if request.url.path.startswith("/v1/projects"):
            if status == "connect_error":
                raise httpx.ConnectError("Server unreachable")
            return httpx.Response(status, json={"detail": "Project not found"})
        return httpx.Response(201, json={"id": uuid4().hex})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps(
                    {"agent_id": "worker", "placement": "same_host", "project_name": "Triage"}
                ),
                server_client=client,
                conversation_id="manager",
            )
        )
    assert error in result.get("error", ""), result
    # No create was sent, so the agent is not told to look for a session that may exist.
    assert "could not be confirmed" not in result["error"]
    assert "/v1/sessions/orchestrated" not in [r.url.path for r in requests]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [404, 500, "connect_error"])
async def test_an_unreadable_project_name_keeps_the_created_session(status):
    session_id = uuid4().hex

    def respond(request):
        if request.url.path == "/v1/projects/inherited":
            if status == "connect_error":
                raise httpx.ConnectError("Server unreachable")
            return httpx.Response(status, json={"detail": "Project not found"})
        return httpx.Response(201, json={"id": session_id, "project_id": "inherited"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(respond), base_url="http://server"
    ) as client:
        result = json.loads(
            await execute_tool(
                tool_name="sys_session_create",
                arguments=json.dumps({"agent_id": "worker", "placement": "same_host"}),
                server_client=client,
                conversation_id="manager",
            )
        )
    # The session exists, so a failed name read leaves only the name unknown.
    assert "error" not in result, result
    assert result["conversation_id"] == session_id
    assert result["project_id"] == "inherited"
    assert result["project_name"] is None
