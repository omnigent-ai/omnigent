"""Runner-side ``sys_journal_*`` tools: registration, retries and older servers.

The tools' behavior against real routes is covered end to end in
``tests/server/integration/test_session_journal_routes.py``.
"""

from __future__ import annotations

import json

import httpx
import jsonschema
import pytest

from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER
from omnigent.runner.journal_tools import JOURNAL_TOOL_NAMES, execute_journal_tool
from omnigent.runner.tool_dispatch import _ALL_LOCAL_TOOLS, _NATIVE_RELAY_BUILTIN_TOOLS
from omnigent.spec.parser import parse_tool_groups
from omnigent.spec.types import AgentSpec
from omnigent.tools import ToolManager
from omnigent.tools.builtins.journal import JOURNAL_TOOLS

SESSION = "4e92b5a0c0ee6db3f874f9c4a3f855a5"
_ENTRY = {
    "id": "a" * 32,
    "position": 0,
    "created_at": 1_762_534_860,
    "scheduled_task_run_id": None,
    "step": None,
    "author": {"type": "agent"},
    "kind": "note",
    "payload": {"body": "hello"},
}


def test_every_agent_gets_the_journal_tools_unless_its_spec_opts_out() -> None:
    names = {
        "sys_journal_read",
        "sys_journal_write_note",
        "sys_journal_write_plan",
        "sys_journal_write_status",
    }
    assert names == JOURNAL_TOOL_NAMES
    assert names <= _ALL_LOCAL_TOOLS and names <= _NATIVE_RELAY_BUILTIN_TOOLS

    def registered(spec: AgentSpec) -> set[str]:
        return {schema["function"]["name"] for schema in ToolManager(spec).get_tool_schemas()}

    assert names <= registered(AgentSpec(spec_version=1))
    groups = parse_tool_groups({"journal": False}, spawn=False)
    assert not names & registered(AgentSpec(spec_version=1, tool_groups=groups))


@pytest.mark.parametrize("tool", JOURNAL_TOOLS)
def test_tool_schemas_are_valid_json_schema(tool: type) -> None:
    schema = tool().get_schema()
    assert schema["function"]["name"] == tool.name()
    jsonschema.Draft202012Validator.check_schema(schema["function"]["parameters"])


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://server.test")


async def _run(client: httpx.AsyncClient, tool: str, args: dict[str, object], **kwargs) -> dict:
    output = await execute_journal_tool(
        tool, json.dumps(args), server_client=client, conversation_id=SESSION, **kwargs
    )
    return json.loads(output)


async def test_a_write_retries_a_dropped_tunnel_with_the_same_key(monkeypatch) -> None:
    monkeypatch.setattr("omnigent.runner.journal_tools._sleep", _no_sleep)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            raise httpx.ConnectError("tunnel reconnecting", request=request)
        if len(requests) == 2:
            return httpx.Response(
                503, json={"error": {"code": "runner_unavailable", "message": "x"}}
            )
        return httpx.Response(200, json=_ENTRY)

    published: list[dict[str, object]] = []
    async with _client(handler) as client:
        result = await _run(
            client,
            "sys_journal_write_note",
            {"body": "hello"},
            call_id="toolu_01",
            publish_event=lambda _session, event: published.append(event),
        )
    assert result == {"id": "a" * 32, "position": 0}
    bodies = [json.loads(request.content) for request in requests[1:]]
    assert [body["idempotency_key"] for body in bodies] == ["tool:toolu_01", "tool:toolu_01"]
    assert all(request.headers[ORCHESTRATION_SOURCE_HEADER] == SESSION for request in requests)
    assert published == [
        {"type": "session.journal.appended", "conversation_id": SESSION, "entry": _ENTRY}
    ]


async def test_calls_without_a_call_id_get_their_own_key() -> None:
    keys: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(json.loads(request.content)["idempotency_key"])
        return httpx.Response(200, json=_ENTRY)

    async with _client(handler) as client:
        for call_id in (None, None, "x" * 200):
            await _run(
                client,
                "sys_journal_write_note",
                {"body": "hi"},
                call_id=call_id,
                publish_event=lambda *_: None,
            )
    assert keys[0] != keys[1] and all(key.startswith("tool:") for key in keys)
    assert len(keys[2]) <= 128 and keys[2].startswith("tool:sha256:")


@pytest.mark.parametrize(
    "body", [{"detail": "Not Found"}, {"error": {"code": "not_found", "message": "Not found"}}]
)
async def test_an_older_server_without_the_journal_says_so(body: dict[str, object]) -> None:
    async with _client(lambda request: httpx.Response(404, json=body)) as client:
        write = await _run(client, "sys_journal_write_note", {"body": "hi"})
        read = await _run(client, "sys_journal_read", {})
    assert write["error"] == "sys_journal_write_note: this Omnigent server has no session journal"
    assert read["error"] == "sys_journal_read: this Omnigent server has no session journal"


async def test_invalid_arguments_never_reach_the_server() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    async with _client(handler) as client:
        assert (await _run(client, "sys_journal_write_status", {"status": "running"}))[
            "error"
        ].startswith("invalid_entry: status must be one of")
        cycle = {"name": "Loop", "steps": [{"id": "a", "name": "A", "depends_on": ["a"]}]}
        assert "cycle" in (await _run(client, "sys_journal_write_plan", cycle))["error"]
        unnamed = {"steps": [{"id": "a", "name": "A", "depends_on": []}]}
        assert (
            "a plan needs a name"
            in (await _run(client, "sys_journal_write_plan", unnamed))["error"]
        )
        assert (await _run(client, "sys_journal_read", {"limit": 101}))["error"].startswith(
            "invalid_entry"
        )
        assert (await _run(client, "sys_journal_read", {"cursor": "!!"}))["error"].startswith(
            "invalid_entry"
        )


async def _no_sleep(_seconds: float) -> None:
    return None
