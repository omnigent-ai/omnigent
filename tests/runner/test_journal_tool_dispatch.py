"""Runner-side journal tools: registration, retries, older servers, and questions.

The tools' behavior against real routes is covered end to end in
``tests/server/integration/test_session_journal_routes.py``.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import jsonschema
import pytest

from omnigent.runner import pending_approvals
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
        "sys_workflow_request_input",
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
        no_options = {"step": "a", "questions": [{"question": "Go?", "header": "Go"}]}
        assert (await _run(client, "sys_workflow_request_input", no_options))["error"].startswith(
            "invalid_input: questions[0].options must list 2 to 4"
        )


async def _no_sleep(_seconds: float) -> None:
    return None


_QUESTION = {
    "question": "Deploy to prod now?",
    "header": "Deploy",
    "options": [{"label": "Deploy"}, {"label": "Wait"}],
    "recommended": "Wait",
}
_STATUS = {**_ENTRY, "kind": "status", "step": "deploy", "payload": {"status": "needs_input"}}


async def _answer_when_asked(elicitation_id: str, *answers: tuple[bool, dict | None]) -> None:
    """Deliver each verdict once the runner waits on the question, as the server would."""
    for approved, content in answers:
        while not pending_approvals.resolve(elicitation_id, approved, content):
            await asyncio.sleep(0.01)


async def test_a_question_waits_for_the_answer_and_returns_it() -> None:
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"entry": _STATUS, "elicitation_id": "elicit_journal_1"})

    published: list[dict[str, object]] = []
    async with _client(handler) as client:
        answer = asyncio.create_task(
            _answer_when_asked("elicit_journal_1", (True, {"q1": "Wait (recommended)"}))
        )
        result = await _run(
            client,
            "sys_workflow_request_input",
            {"step": "deploy", "questions": [_QUESTION]},
            call_id="toolu_9",
            publish_event=lambda _session, event: published.append(event),
        )
        await answer
    assert result == {
        "answered": True,
        "answers": [{"question": "Deploy to prod now?", "answer": "Wait"}],
    }
    assert requests == [
        {
            "step": "deploy",
            "questions": [{**_QUESTION, "options": [{"label": "Deploy"}, {"label": "Wait"}]}],
            "idempotency_key": "tool:toolu_9",
        }
    ]
    # The task's status reaches the Tasks tab while the question waits.
    assert published[0] == {
        "type": "session.journal.appended",
        "conversation_id": SESSION,
        "entry": _STATUS,
    }


async def test_a_question_is_raised_again_after_the_server_reconnects() -> None:
    keys: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(json.loads(request.content)["idempotency_key"])
        return httpx.Response(200, json={"entry": _STATUS, "elicitation_id": "elicit_journal_2"})

    async def reconnect_then_answer() -> None:
        while not pending_approvals.has_pending(SESSION):
            await asyncio.sleep(0.01)
        pending_approvals.notify_server_reconnect()
        while len(keys) < 2:
            await asyncio.sleep(0.01)
        await _answer_when_asked("elicit_journal_2", (True, {"q1": "Deploy"}))

    async with _client(handler) as client:
        helper = asyncio.create_task(reconnect_then_answer())
        result = await _run(
            client,
            "sys_workflow_request_input",
            {"step": "deploy", "questions": [_QUESTION]},
            call_id="toolu_10",
            publish_event=lambda *_: None,
        )
        await helper
    assert keys == ["tool:toolu_10", "tool:toolu_10"]
    assert result["answers"] == [{"question": "Deploy to prod now?", "answer": "Deploy"}]


async def test_a_dismissed_question_tells_the_agent_not_to_ask_again() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"entry": _STATUS, "elicitation_id": "elicit_journal_3"})

    async with _client(handler) as client:
        answer = asyncio.create_task(_answer_when_asked("elicit_journal_3", (False, None)))
        result = await _run(
            client,
            "sys_workflow_request_input",
            {"step": "deploy", "questions": [_QUESTION]},
            publish_event=lambda *_: None,
        )
        await answer
    assert result["answered"] is False
    assert "don't ask again this turn" in result["detail"]
