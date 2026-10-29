"""Tests for the scheduled-task (``sys_scheduled_task_*``) tool surface.

Covers the runner-side half:

- ``_execute_scheduled_task_tool``: the create/list/update/delete calls proxy
  the correct ``/v1/scheduled-tasks`` REST verb + payload over ``server_client``,
  pass the JSON body through verbatim, and translate a server 4xx/5xx and a
  missing id into clean error JSON.
- Registration: the four ``sys_scheduled_task_*`` names are always registered by
  ``ToolManager`` (no spec opt-in) and are members of the local-dispatch and
  native-relay tool sets.
"""

from __future__ import annotations

import json

import jsonschema
import pytest

from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _SCHEDULED_TASK_TOOLS,
    _execute_scheduled_task_tool,
)
from omnigent.tools.builtins.scheduled_tasks import SysScheduledTaskUpdateTool

_ALL_NAMES = {
    "sys_scheduled_task_create",
    "sys_scheduled_task_list",
    "sys_scheduled_task_update",
    "sys_scheduled_task_delete",
}
_TASK_ID = "0123456789abcdef0123456789abcdef"


class _Resp:
    def __init__(self, *, status_code: int = 200, body: object | None = None) -> None:
        self.status_code = status_code
        self._body = body if body is not None else {}

    @property
    def text(self) -> str:
        return json.dumps(self._body)

    def json(self) -> object:
        return self._body


class _RecordingClient:
    """Records the verb/url/json of each call and returns a scripted response.

    ``info`` answers ``/v1/info``; by default a server that accepts task
    projects and session modes.
    """

    def __init__(self, response: _Resp | None = None, *, info: _Resp | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []
        self._response = response or _Resp(body={"id": "t1"})
        self._info = info or _Resp(
            body={"scheduled_task_projects": True, "scheduled_task_session_modes": True}
        )

    async def get(self, url: str, *, timeout: object = None) -> _Resp:
        self.calls.append(("GET", url, None))
        return self._info if url == "/v1/info" else self._response

    async def post(self, url: str, *, json: dict | None = None, timeout: object = None) -> _Resp:
        self.calls.append(("POST", url, json))
        return self._response

    async def patch(self, url: str, *, json: dict | None = None, timeout: object = None) -> _Resp:
        self.calls.append(("PATCH", url, json))
        return self._response

    async def delete(self, url: str, *, timeout: object = None) -> _Resp:
        self.calls.append(("DELETE", url, None))
        return self._response


@pytest.mark.asyncio
async def test_create_posts_payload() -> None:
    client = _RecordingClient(_Resp(body={"id": "t1", "name": "nightly"}))
    out = await _execute_scheduled_task_tool(
        "sys_scheduled_task_create",
        json.dumps(
            {
                "name": "nightly",
                "prompt": "go",
                "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
                "agent_id": "ag_1",
                "workspace": "/repo",
                "host_id": "host_1",
                "project_id": "proj_1",
                "session_mode": "continue",
                "session_id": "conv_1",
                "base_branch": "main",
                "unexpected": "dropped",
            }
        ),
        server_client=client,
    )
    verb, url, body = client.calls[-1]
    assert (verb, url) == ("POST", "/v1/scheduled-tasks")
    assert body == {
        "name": "nightly",
        "prompt": "go",
        "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        "agent_id": "ag_1",
        "workspace": "/repo",
        "host_id": "host_1",
        "project_id": "proj_1",
        "session_mode": "continue",
        "session_id": "conv_1",
    }  # unknown fields filtered out
    assert json.loads(out)["id"] == "t1"


@pytest.mark.asyncio
async def test_create_without_session_args_sends_no_session_fields() -> None:
    """An older server rejects unknown fields, so a plain create must not send them."""
    client = _RecordingClient(_Resp(body={"id": "t1"}))
    await _execute_scheduled_task_tool(
        "sys_scheduled_task_create",
        json.dumps({"name": "n", "prompt": "p", "rrule": "FREQ=DAILY", "agent_id": "ag_1"}),
        server_client=client,
    )
    _, _, body = client.calls[0]
    assert not {"session_mode", "session_id"} & set(body)
    assert "/v1/info" not in [url for _, url, _ in client.calls]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "info",
    [_Resp(body={"scheduled_task_projects": True}), _Resp(status_code=500)],
    ids=["missing", "info-fails"],
)
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        (
            "sys_scheduled_task_create",
            {
                "name": "n",
                "prompt": "p",
                "rrule": "FREQ=DAILY",
                "agent_id": "ag_1",
                "session_mode": "continue",
            },
        ),
        ("sys_scheduled_task_update", {"scheduled_task_id": _TASK_ID, "session_id": None}),
    ],
)
async def test_session_args_are_refused_before_an_older_server_sees_them(
    tool_name: str, arguments: dict[str, object], info: _Resp
) -> None:
    """A server without session modes would reject the fields with a bare 422."""
    client = _RecordingClient(info=info)
    out = await _execute_scheduled_task_tool(
        tool_name, json.dumps(arguments), server_client=client
    )

    assert [url for _, url, _ in client.calls] == ["/v1/info"]
    assert "does not support session_mode or session_id" in json.loads(out)["error"]


@pytest.mark.asyncio
async def test_list_gets() -> None:
    client = _RecordingClient(_Resp(body={"scheduled_tasks": []}))
    out = await _execute_scheduled_task_tool("sys_scheduled_task_list", "", server_client=client)
    assert client.calls[0] == ("GET", "/v1/scheduled-tasks", None)
    assert json.loads(out) == {"scheduled_tasks": []}


@pytest.mark.asyncio
async def test_update_patches_by_id() -> None:
    client = _RecordingClient(_Resp(body={"id": "t1", "state": "paused"}))
    await _execute_scheduled_task_tool(
        "sys_scheduled_task_update",
        json.dumps(
            {
                "scheduled_task_id": _TASK_ID,
                "state": "paused",
                "workspace": "/repo2",
                "host_id": "fedcba9876543210fedcba9876543210",
            }
        ),
        server_client=client,
    )
    verb, url, body = client.calls[0]
    assert (verb, url) == ("PATCH", f"/v1/scheduled-tasks/{_TASK_ID}")
    assert body == {
        "state": "paused",
        "workspace": "/repo2",
        "host_id": "fedcba9876543210fedcba9876543210",
    }


@pytest.mark.asyncio
async def test_update_forwards_agent_switch_cost_cap_and_project() -> None:
    """``agent_id`` / ``max_cost_usd`` / ``project_id`` reach the PATCH instead of being dropped.

    The dispatch allowlist is what an agent's ``sys_scheduled_task_update`` call
    actually travels through, so a field missing from it is silently ignored —
    the update appears to succeed while changing nothing.
    """
    client = _RecordingClient(_Resp(body={"id": "t1"}))
    await _execute_scheduled_task_tool(
        "sys_scheduled_task_update",
        json.dumps(
            {
                "scheduled_task_id": _TASK_ID,
                "agent_id": "ag_pi",
                "max_cost_usd": 2.5,
                "project_id": None,
            }
        ),
        server_client=client,
    )
    _, _, body = client.calls[-1]
    assert body == {"agent_id": "ag_pi", "max_cost_usd": 2.5, "project_id": None}


class _ProjectsClient(_RecordingClient):
    """Serves ``/v1/projects`` from a list; other calls get the scripted response."""

    def __init__(
        self,
        projects: list[dict[str, str]],
        *,
        create_status: int = 200,
        info: _Resp | None = None,
    ) -> None:
        super().__init__(info=info)
        self.projects = projects
        self.create_status = create_status

    async def get(self, url: str, *, timeout: object = None) -> _Resp:
        if url != "/v1/projects":
            return await super().get(url, timeout=timeout)
        self.calls.append(("GET", url, None))
        return _Resp(body={"data": list(self.projects)})

    async def post(self, url: str, *, json: dict | None = None, timeout: object = None) -> _Resp:
        if url != "/v1/projects":
            return await super().post(url, json=json, timeout=timeout)
        self.calls.append(("POST", url, json))
        if self.create_status == 409:
            # Another request created the same name first.
            self.projects.append({"id": "proj_racer", "name": json["name"]})
            return _Resp(status_code=409, body={"detail": "name taken"})
        if self.create_status >= 400:
            return _Resp(status_code=self.create_status, body={"detail": "boom"})
        self.projects.append({"id": "proj_new", "name": json["name"]})
        return _Resp(body={"id": "proj_new", "name": json["name"]})


_EXISTING = [{"id": "proj_other", "name": "Triage"}, {"id": "proj_triage", "name": "triage"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "projects", "create_status", "filed_into", "created"),
    [
        ("sys_scheduled_task_create", _EXISTING, 200, "proj_triage", False),
        ("sys_scheduled_task_create", [], 200, "proj_new", True),
        ("sys_scheduled_task_update", [], 409, "proj_racer", True),
    ],
    ids=["reuses-exact-name", "creates-missing", "reuses-race-winner"],
)
async def test_project_name_is_sent_as_the_resolved_project_id(
    tool_name: str,
    projects: list[dict[str, str]],
    create_status: int,
    filed_into: str,
    created: bool,
) -> None:
    client = _ProjectsClient(list(projects), create_status=create_status)
    args: dict[str, object] = {"project_name": "  triage "}
    if tool_name == "sys_scheduled_task_update":
        args["scheduled_task_id"] = _TASK_ID
    await _execute_scheduled_task_tool(tool_name, json.dumps(args), server_client=client)

    creates = [body for verb, url, body in client.calls if (verb, url) == ("POST", "/v1/projects")]
    assert creates == ([{"name": "triage"}] if created else [])
    _, url, body = client.calls[-1]
    assert url.startswith("/v1/scheduled-tasks")
    assert body is not None and body["project_id"] == filed_into
    assert "project_name" not in body


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("args", "create_status", "error"),
    [
        ({"project_name": "a", "project_id": "p"}, 200, "not both"),
        ({"project_name": "   "}, 200, "must not be empty"),
        ({"project_name": None}, 200, "must be a string"),
        ({"project_name": "a"}, 500, "could not create project"),
    ],
    ids=["both-fields", "blank-name", "null-name", "create-fails"],
)
async def test_project_name_errors_never_reach_the_task_route(
    args: dict[str, object], create_status: int, error: str
) -> None:
    client = _ProjectsClient([], create_status=create_status)
    out = await _execute_scheduled_task_tool(
        "sys_scheduled_task_create", json.dumps(args), server_client=client
    )
    assert error in json.loads(out)["error"]
    assert not [url for _, url, _ in client.calls if url.startswith("/v1/scheduled-tasks")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "info",
    [_Resp(body={}), _Resp(body={"scheduled_task_projects": False}), _Resp(status_code=500)],
    ids=["missing", "false", "info-fails"],
)
@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("sys_scheduled_task_create", {"project_name": "new"}),
        ("sys_scheduled_task_create", {"project_id": "proj_1"}),
        ("sys_scheduled_task_update", {"scheduled_task_id": _TASK_ID, "project_id": None}),
    ],
    ids=["create-by-name", "create-by-id", "update-clear"],
)
async def test_project_args_are_refused_on_a_server_without_task_projects(
    info: _Resp, tool_name: str, args: dict[str, object]
) -> None:
    """An older server would 422 the field, so nothing is created or sent."""
    client = _ProjectsClient([], info=info)
    out = await _execute_scheduled_task_tool(tool_name, json.dumps(args), server_client=client)
    assert "does not support automation projects" in json.loads(out)["error"]
    assert [url for _, url, _ in client.calls] == ["/v1/info"]


@pytest.mark.asyncio
async def test_update_by_name_on_an_unknown_task_creates_no_project() -> None:
    class _MissingTaskClient(_ProjectsClient):
        async def get(self, url: str, *, timeout: object = None) -> _Resp:
            if url.startswith("/v1/scheduled-tasks/"):
                self.calls.append(("GET", url, None))
                return _Resp(status_code=404, body={"detail": "Scheduled task not found"})
            return await super().get(url, timeout=timeout)

    client = _MissingTaskClient([])
    out = await _execute_scheduled_task_tool(
        "sys_scheduled_task_update",
        json.dumps({"scheduled_task_id": _TASK_ID, "project_name": "new"}),
        server_client=client,
    )
    assert json.loads(out)["error"] == "server returned 404"
    assert not [call for call in client.calls if call[1] == "/v1/projects"]
    assert not [call for call in client.calls if call[0] == "PATCH"]


@pytest.mark.asyncio
async def test_calls_without_project_args_skip_the_capability_check() -> None:
    client = _RecordingClient(info=_Resp(body={}))
    await _execute_scheduled_task_tool(
        "sys_scheduled_task_update",
        json.dumps({"scheduled_task_id": _TASK_ID, "name": "renamed"}),
        server_client=client,
    )
    assert [url for _, url, _ in client.calls] == [f"/v1/scheduled-tasks/{_TASK_ID}"]


@pytest.mark.parametrize("project_id", ["0123456789abcdef0123456789abcdef", None])
def test_update_schema_accepts_a_project_id_or_null(project_id: str | None) -> None:
    """A schema-validating client can move a task's project or clear it with null."""
    parameters = SysScheduledTaskUpdateTool().get_schema()["function"]["parameters"]
    jsonschema.validate({"scheduled_task_id": _TASK_ID, "project_id": project_id}, parameters)


@pytest.mark.asyncio
async def test_delete_by_id() -> None:
    client = _RecordingClient(_Resp(body={"deleted": True, "id": "t1"}))
    await _execute_scheduled_task_tool(
        "sys_scheduled_task_delete",
        json.dumps({"scheduled_task_id": _TASK_ID.upper()}),
        server_client=client,
    )
    assert client.calls[0] == ("DELETE", f"/v1/scheduled-tasks/{_TASK_ID}", None)


@pytest.mark.asyncio
async def test_update_rejects_path_confusion_task_id() -> None:
    client = _RecordingClient()
    out = await _execute_scheduled_task_tool(
        "sys_scheduled_task_update",
        json.dumps(
            {"scheduled_task_id": "../0123456789abcdef0123456789abcdef", "state": "paused"}
        ),
        server_client=client,
    )
    assert "canonical 32-character hex" in json.loads(out)["error"]
    assert client.calls == []


@pytest.mark.asyncio
async def test_update_without_id_errors() -> None:
    client = _RecordingClient()
    out = await _execute_scheduled_task_tool(
        "sys_scheduled_task_update", json.dumps({"state": "paused"}), server_client=client
    )
    assert "scheduled_task_id" in json.loads(out)["error"]
    assert client.calls == []  # never hit the server


@pytest.mark.asyncio
async def test_server_error_becomes_clean_json() -> None:
    client = _RecordingClient(_Resp(status_code=400, body={"error": {"message": "bad rrule"}}))
    out = await _execute_scheduled_task_tool(
        "sys_scheduled_task_create",
        json.dumps({"name": "x", "prompt": "p", "rrule": "FREQ=SECONDLY", "agent_id": "a"}),
        server_client=client,
    )
    assert "server returned 400" in json.loads(out)["error"]


@pytest.mark.asyncio
async def test_no_server_client_errors() -> None:
    out = await _execute_scheduled_task_tool("sys_scheduled_task_list", "", server_client=None)
    assert "requires server access" in json.loads(out)["error"]


def test_tools_registered_without_spec_optin() -> None:
    """All four tools register on a minimal spec (always-on, like policy)."""
    from omnigent.spec.types import AgentSpec
    from omnigent.tools.manager import ToolManager

    mgr = ToolManager(AgentSpec(spec_version=1))
    names = {s["function"]["name"] for s in mgr.get_tool_schemas()}
    assert names >= _ALL_NAMES


def test_create_tool_schema_makes_workspace_and_host_optional() -> None:
    from omnigent.tools.builtins.scheduled_tasks import SysScheduledTaskCreateTool

    schema = SysScheduledTaskCreateTool().get_schema()["function"]["parameters"]
    properties = schema["properties"]
    # workspace / host_id stay available as optional properties (a task that
    # does code work still pins them), but are no longer required — a
    # no-workspace research / summary / chat-only task omits both.
    assert "workspace" in properties
    assert "host_id" in properties
    assert "base_branch" not in properties
    assert "workspace" not in schema["required"]
    assert "host_id" not in schema["required"]
    assert set(schema["required"]) == {"name", "prompt", "rrule", "agent_id"}


def test_update_tool_schema_allows_connected_host_changes() -> None:
    from omnigent.tools.builtins.scheduled_tasks import SysScheduledTaskUpdateTool

    schema = SysScheduledTaskUpdateTool().get_schema()["function"]["parameters"]
    properties = schema["properties"]
    assert "workspace" in properties
    assert "host_id" in properties
    # Switching which agent/harness a task runs is part of the update surface.
    assert "agent_id" in properties


def test_tools_in_dispatch_and_relay_sets() -> None:
    assert _SCHEDULED_TASK_TOOLS == _ALL_NAMES
    assert _ALL_NAMES <= _ALL_LOCAL_TOOLS
    assert _ALL_NAMES <= _NATIVE_RELAY_BUILTIN_TOOLS


@pytest.mark.asyncio
async def test_update_forwards_session_fields() -> None:
    """``session_mode`` and a null ``session_id`` reach the PATCH body."""
    client = _RecordingClient(_Resp(body={"id": "t1"}))
    await _execute_scheduled_task_tool(
        "sys_scheduled_task_update",
        json.dumps(
            {
                "scheduled_task_id": _TASK_ID,
                "session_mode": "continue",
                "session_id": None,
            }
        ),
        server_client=client,
    )
    _, _, body = client.calls[-1]
    assert body == {"session_mode": "continue", "session_id": None}
