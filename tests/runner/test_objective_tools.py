"""The objective tools: when they register, what they send, and the automation gate.

* ``ToolManager`` registers ``sys_record_objective`` and ``sys_record_outcome``
  only when ``tool_groups.objectives`` is ``True``; the default ``None`` leaves
  the choice to the runner, which turns them on in automation sessions.
* The native relay carries them when they are registered.
* The runner sends them for the calling session only, and explains an older
  server.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.automation_session import (
    serves_automation,
    session_agent_revision,
    with_automation_tools,
)
from omnigent.errors import OmnigentError
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _execute_objective_tool,
    build_native_relay_tool_schemas,
)
from omnigent.spec.parser import parse_tool_groups
from omnigent.spec.types import AgentSpec, ToolGroupsConfig
from omnigent.stores.conversation_store import (
    SCHEDULED_TASK_ORIGIN_LABEL_KEY,
    SCHEDULED_TASK_RUN_LABEL_KEY,
)
from omnigent.tools.manager import ToolManager

_NAMES = {"sys_record_objective", "sys_record_outcome"}
_SESSION_ID = "fedcba9876543210fedcba9876543210"


def _spec(objectives: bool | None) -> AgentSpec:
    return AgentSpec(
        spec_version=1, name="objectives", tool_groups=ToolGroupsConfig(objectives=objectives)
    )


def _registered(spec: AgentSpec) -> set[str]:
    manager = ToolManager(spec, os_env_schema_only=True)
    try:
        return set(manager.get_tool_names())
    finally:
        manager.shutdown()


@pytest.mark.parametrize(
    ("objectives", "registered"), [(None, False), (False, False), (True, True)]
)
def test_the_tools_register_only_when_the_group_is_on(
    objectives: bool | None, registered: bool
) -> None:
    spec = _spec(objectives)
    expected = _NAMES if registered else set()
    assert _NAMES & _registered(spec) == expected
    relayed = {schema["name"] for schema in build_native_relay_tool_schemas(spec)}
    assert _NAMES & relayed == expected


def test_the_tools_are_dispatched_by_the_runner_and_relayed() -> None:
    assert _NAMES <= _ALL_LOCAL_TOOLS
    assert _NAMES <= _NATIVE_RELAY_BUILTIN_TOOLS


def test_tool_groups_accepts_a_boolean_for_objectives() -> None:
    assert parse_tool_groups(None, spawn=False).objectives is None
    assert parse_tool_groups({"objectives": True}, spawn=False).objectives is True
    assert parse_tool_groups({"objectives": False}, spawn=False).objectives is False
    with pytest.raises(OmnigentError, match="must be a boolean"):
        parse_tool_groups({"objectives": "auto"}, spawn=False)


@pytest.mark.parametrize(
    ("labels", "automation"),
    [
        ({}, False),
        ({SCHEDULED_TASK_ORIGIN_LABEL_KEY: "task"}, True),
        ({SCHEDULED_TASK_RUN_LABEL_KEY: "run"}, True),
        ({SCHEDULED_TASK_ORIGIN_LABEL_KEY: ""}, False),
    ],
)
def test_an_automation_session_changes_its_agent_revision(
    labels: dict[str, str], automation: bool
) -> None:
    """The runner rebuilds the spec when an automation starts serving a session."""
    assert serves_automation(labels) is automation
    revision = session_agent_revision("bundle-1", labels)
    assert (revision != session_agent_revision("bundle-1", {})) is automation


@pytest.mark.parametrize(
    ("objectives", "automation", "expected"),
    [(None, True, True), (None, False, None), (False, True, False), (True, False, True)],
)
def test_only_an_undecided_spec_follows_the_session(
    objectives: bool | None, automation: bool, expected: bool | None
) -> None:
    spec = _spec(objectives)
    adjusted = with_automation_tools(spec, automation=automation)
    assert adjusted.tool_groups.objectives is expected
    # The shared cached spec is never changed in place.
    assert spec.tool_groups.objectives is objectives


class _Client:
    """Records requests and answers with one fixed response."""

    def __init__(self, response: httpx.Response) -> None:
        self.calls: list[tuple[str, str, Any, dict[str, str]]] = []
        self._response = response

    async def post(self, url: str, *, json: Any, headers: dict[str, str], timeout: float):
        self.calls.append(("POST", url, json, headers))
        return self._response

    async def put(self, url: str, *, json: Any, headers: dict[str, str], timeout: float):
        self.calls.append(("PUT", url, json, headers))
        return self._response


def _recorded(**overrides: object) -> httpx.Response:
    body: dict[str, object] = {"id": "o1", "objective": "Ship it", "outcome": None}
    body.update(overrides)
    return httpx.Response(200, json=body)


async def _call(client: _Client, tool: str, args: object, session: str | None = _SESSION_ID):
    out = await _execute_objective_tool(
        tool,
        json.dumps(args),
        server_client=client,  # type: ignore[arg-type]
        conversation_id=session,
    )
    return json.loads(out)


@pytest.mark.asyncio
async def test_objectives_and_outcomes_are_sent_for_the_calling_session() -> None:
    """A session id in the arguments is never forwarded: the caller's own is used."""
    client = _Client(_recorded())
    assert await _call(
        client, "sys_record_objective", {"objective": "Ship it", "session_id": "0" * 32}
    ) == {"recorded": True, "objective_id": "o1", "objective": "Ship it", "outcome": None}
    outcome = {"success": False, "emoji": "🚧", "text": "Blocked"}
    await _call(client, "sys_record_outcome", outcome)
    await _call(client, "sys_record_outcome", {**outcome, "objective_id": "o1"})

    url = f"/v1/sessions/{_SESSION_ID}/objectives"
    assert [(method, path, body) for method, path, body, _ in client.calls] == [
        ("POST", url, {"objective": "Ship it"}),
        ("PUT", f"{url}/outcome", outcome),
        ("PUT", f"{url}/outcome", {**outcome, "objective_id": "o1"}),
    ]
    assert {tuple(headers.items()) for *_, headers in client.calls} == {
        ((ORCHESTRATION_SOURCE_HEADER, _SESSION_ID),)
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(405), "does not record objectives"),
        (httpx.Response(404, json={"detail": "Not Found"}), "does not record objectives"),
        (
            httpx.Response(404, json={"error": {"code": "not_found", "message": "Not found"}}),
            "does not record objectives",
        ),
        (
            httpx.Response(
                404,
                json={"error": {"code": "not_found", "message": "Objective not found"}},
            ),
            "server returned 404",
        ),
        (httpx.Response(422, json={"detail": "bad emoji"}), "invalid arguments"),
        (httpx.Response(409, json={"detail": "full"}), "server returned 409"),
    ],
)
async def test_server_refusals_are_explained(response: httpx.Response, message: str) -> None:
    out = await _call(
        _Client(response), "sys_record_outcome", {"success": True, "emoji": "✅", "text": "x"}
    )
    assert message in out["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool", "args", "session", "message"),
    [
        ("sys_record_objective", {"objective": "x"}, None, "needs the calling session"),
        ("sys_record_objective", {"objective": 3}, _SESSION_ID, "must be a string"),
        ("sys_record_objective", [], _SESSION_ID, "must be an object"),
        (
            "sys_record_outcome",
            {"success": "yes", "emoji": "✅", "text": "x"},
            _SESSION_ID,
            "true or false",
        ),
        ("sys_record_outcome", {"success": True, "emoji": 1, "text": "x"}, _SESSION_ID, "strings"),
        (
            "sys_record_outcome",
            {"success": True, "emoji": "✅", "text": "x", "objective_id": 1},
            _SESSION_ID,
            "objective_id must be a string",
        ),
    ],
)
async def test_bad_calls_never_reach_the_server(
    tool: str, args: object, session: str | None, message: str
) -> None:
    client = _Client(_recorded())
    assert message in (await _call(client, tool, args, session))["error"]
    assert client.calls == []
