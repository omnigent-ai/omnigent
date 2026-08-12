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

import pytest

from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _SCHEDULED_TASK_TOOLS,
    _execute_scheduled_task_tool,
)

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
    """Records the verb/url/json of each call and returns a scripted response."""

    def __init__(self, response: _Resp | None = None) -> None:
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []
        self._response = response or _Resp(body={"id": "t1"})

    async def get(self, url: str, *, timeout: object = None) -> _Resp:
        self.calls.append(("GET", url, None))
        return self._response

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
                "base_branch": "main",
                "unexpected": "dropped",
            }
        ),
        server_client=client,
    )
    verb, url, body = client.calls[0]
    assert (verb, url) == ("POST", "/v1/scheduled-tasks")
    assert body == {
        "name": "nightly",
        "prompt": "go",
        "rrule": "FREQ=DAILY;BYHOUR=9;BYMINUTE=0",
        "agent_id": "ag_1",
        "workspace": "/repo",
        "host_id": "host_1",
    }  # unknown fields filtered out
    assert json.loads(out)["id"] == "t1"


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


def test_tools_in_dispatch_and_relay_sets() -> None:
    assert _SCHEDULED_TASK_TOOLS == _ALL_NAMES
    assert _ALL_NAMES <= _ALL_LOCAL_TOOLS
    assert _ALL_NAMES <= _NATIVE_RELAY_BUILTIN_TOOLS
