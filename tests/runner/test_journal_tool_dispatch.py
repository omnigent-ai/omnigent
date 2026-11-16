"""Runner-side journal tools: registration, retries, older servers, questions, workflows.

The tools' behavior against real routes is covered end to end in
``tests/server/integration/test_session_journal_routes.py``.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

import httpx
import jsonschema
import pytest

from omnigent.entities.workflow_questions import workflow_elicitation_id
from omnigent.runner import journal_tools, pending_approvals
from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER, RUNNER_TUNNEL_TOKEN_HEADER
from omnigent.runner.journal_tools import (
    JOURNAL_TOOL_NAMES,
    execute_journal_tool,
    invalidate_server_workflow_flag,
    server_workflows_enabled,
)
from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    build_native_relay_tool_schemas,
    filter_workflow_tool_schemas,
)
from omnigent.runtime import pending_elicitations
from omnigent.spec.parser import parse_tool_groups
from omnigent.spec.types import AgentSpec
from omnigent.tools import ToolManager
from omnigent.tools.builtins.journal import JOURNAL_TOOLS
from tests._helpers.polling import poll_until_async

SESSION = "4e92b5a0c0ee6db3f874f9c4a3f855a5"
_ENTRY = {
    "id": "a" * 32,
    "position": 0,
    "created_at": 1_762_534_860,
    "scheduled_task_run_id": None,
    "step": None,
    "author": {"type": "agent"},
    "kind": "note",
    "content": {"body": "hello"},
}


def test_every_agent_gets_the_journal_tools_unless_its_spec_opts_out() -> None:
    names = {
        "sys_journal_read",
        "sys_journal_write_note",
        "sys_journal_write_plan",
        "sys_journal_write_status",
        "sys_workflow_request_input",
        "sys_workflow_save",
        "sys_workflow_load",
        "sys_workflow_list",
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


@pytest.mark.parametrize(
    ("info", "enabled"),
    [
        ({}, False),
        ({"workflows": True}, False),
        ({"features": {"workflows": False}}, False),
        ({"features": {"workflows": "true"}}, False),
        ({"features": {"workflows": True}}, True),
    ],
)
@pytest.mark.parametrize("native", [False, True])
async def test_workflow_tool_advertisement_follows_the_server_not_the_host(
    monkeypatch: pytest.MonkeyPatch, info: dict, enabled: bool, native: bool
) -> None:
    monkeypatch.setenv("OMNIGENT_FEATURES", "workflows" if not enabled else "-workflows")
    spec = AgentSpec(spec_version=1)
    schemas = (
        build_native_relay_tool_schemas(spec) if native else ToolManager(spec).get_tool_schemas()
    )
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=info)

    async with _client(respond) as client:
        visible = filter_workflow_tool_schemas(
            schemas, enabled=await server_workflows_enabled(client)
        )
        assert await server_workflows_enabled(client) is enabled
        assert len(requests) == 1
    names = {schema["name"] if native else schema["function"]["name"] for schema in visible}
    assert names & JOURNAL_TOOL_NAMES == (JOURNAL_TOOL_NAMES if enabled else set())
    assert "sys_scheduled_task_create" in names
    assert "sys_session_list" in names


@pytest.mark.parametrize("failure", ["unavailable", "invalid_json", "offline", "read_timeout"])
async def test_failed_workflow_flag_probes_retry_and_preserve_last_success(failure: str) -> None:
    unavailable, enabled = True, True
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert 0 < request.extensions["timeout"]["read"] <= 5
        if not unavailable:
            return httpx.Response(200, json={"features": {"workflows": enabled}})
        if failure == "read_timeout":
            raise httpx.ReadTimeout("no response", request=request)
        if failure == "offline":
            raise httpx.ConnectError("offline", request=request)
        if failure == "invalid_json":
            return httpx.Response(200, text="not json")
        return httpx.Response(503)

    async with _client(respond) as client:
        client.timeout = httpx.Timeout(5.0, read=None)
        invalidate_server_workflow_flag(client)
        assert not requests
        assert await server_workflows_enabled(client) is False
        unavailable = False
        assert await server_workflows_enabled(client) is True
        assert len(requests) == 2
        unavailable = True
        assert await server_workflows_enabled(client) is True
        assert len(requests) == 2
        invalidate_server_workflow_flag(client)
        assert len(requests) == 2
        assert await server_workflows_enabled(client) is True
        assert len(requests) == 3
        unavailable, enabled = False, False
        assert await server_workflows_enabled(client) is False
        assert await server_workflows_enabled(client) is False
        assert len(requests) == 4
        enabled = True
        invalidate_server_workflow_flag(client)
        assert len(requests) == 4
        assert await server_workflows_enabled(client) is True
        assert len(requests) == 5


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
        client.headers[RUNNER_TUNNEL_TOKEN_HEADER] = "journal-test-runner"
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
    assert all(
        request.headers[RUNNER_TUNNEL_TOKEN_HEADER] == "journal-test-runner"
        for request in requests
    )
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


@pytest.mark.parametrize(
    "message",
    [
        "Workflows are disabled on this server",
        "Session journals are not available on this server",
        "Session not found",
        "Conversation not found",
    ],
)
@pytest.mark.parametrize("operation", ["read", "write", "request_input"])
async def test_journal_404s_preserve_the_servers_reason(message: str, operation: str) -> None:
    tool, args = {
        "read": ("sys_journal_read", {}),
        "write": ("sys_journal_write_note", {"body": "hi"}),
        "request_input": (
            "sys_workflow_request_input",
            {"step": "deploy", "questions": [_QUESTION]},
        ),
    }[operation]
    response = httpx.Response(404, json={"error": {"code": "not_found", "message": message}})
    async with _client(lambda request: response) as client:
        result = await _run(client, tool, args)
    assert result == (
        {"error": "session_not_found", "conversation_id": SESSION}
        if message in {"Session not found", "Conversation not found"}
        else {"error": message}
    )


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
        stray = {**unnamed, "name": "Run", "workflow_id": "Triage"}
        assert (await _run(client, "sys_journal_write_plan", stray))["error"] == (
            "invalid_entry: workflow_id must be a saved workflow's ID"
        )
        assert (await _run(client, "sys_journal_read", {"limit": 101}))["error"].startswith(
            "invalid_entry"
        )
        assert (await _run(client, "sys_journal_read", {"cursor": "!!"}))["error"].startswith(
            "invalid_entry"
        )
        assert (await _run(client, "sys_workflow_list", {"limit": 51}))["error"].startswith(
            "invalid_input: limit"
        )
        assert (await _run(client, "sys_workflow_list", {"cursor": "!!"}))["error"].startswith(
            "invalid_input: cursor"
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
_STATUS = {**_ENTRY, "kind": "status", "step": "deploy", "content": {"status": "needs_input"}}


async def _answer_when_asked(elicitation_id: str, *answers: tuple[bool, dict | None]) -> None:
    """Deliver each verdict once the runner waits on the question, as the server would."""
    for approved, content in answers:
        while not pending_approvals.resolve(elicitation_id, approved, content):
            await asyncio.sleep(0.01)


@pytest.mark.parametrize("answer_before_response", [False, True])
async def test_a_question_waits_for_the_answer_and_returns_it(
    answer_before_response: bool,
) -> None:
    requests: list[dict[str, object]] = []
    elicitation_id = workflow_elicitation_id(SESSION, "tool:toolu_9")
    content = {"Deploy to prod now?": "Wait (recommended)"}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        if answer_before_response:
            assert pending_approvals.has_pending(SESSION)
            assert pending_approvals.resolve(elicitation_id, True, content)
        return httpx.Response(200, json={"entry": _STATUS, "elicitation_id": elicitation_id})

    published: list[dict[str, object]] = []
    async with _client(handler) as client:
        answer = (
            None
            if answer_before_response
            else asyncio.create_task(_answer_when_asked(elicitation_id, (True, content)))
        )
        result = await _run(
            client,
            "sys_workflow_request_input",
            {"step": "deploy", "questions": [_QUESTION]},
            call_id="toolu_9",
            publish_event=lambda _session, event: published.append(event),
        )
        if answer is not None:
            await answer
    assert not pending_approvals.has_pending(SESSION)
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
    elicitation_id = workflow_elicitation_id(SESSION, "tool:toolu_10")
    first_question = asyncio.Event()
    replacement_question = asyncio.Event()
    delayed_events: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(json.loads(request.content)["idempotency_key"])
        pending_elicitations.record_publish(
            SESSION, {"type": "response.elicitation_request", "elicitation_id": elicitation_id}
        )
        (first_question if len(keys) == 1 else replacement_question).set()
        return httpx.Response(200, json={"entry": _STATUS, "elicitation_id": elicitation_id})

    async with _client(handler) as client:
        waiting = asyncio.create_task(
            _run(
                client,
                "sys_workflow_request_input",
                {"step": "deploy", "questions": [_QUESTION]},
                call_id="toolu_10",
                publish_event=lambda _session, event: delayed_events.append(event),
            )
        )
        try:
            await asyncio.wait_for(first_question.wait(), timeout=5)
            pending_approvals.notify_server_reconnect()
            await asyncio.wait_for(replacement_question.wait(), timeout=5)
            # Runner stream delivery can lag behind the replacement HTTP request.
            for event in delayed_events:
                pending_elicitations.record_publish(SESSION, event)
            delayed_events.clear()
            assert pending_elicitations.count_for(SESSION) == 1
            assert pending_approvals.has_pending(SESSION)
            assert pending_approvals.resolve(
                elicitation_id, True, {"Deploy to prod now?": "Deploy"}
            )
            result = await asyncio.wait_for(waiting, timeout=5)
            for event in delayed_events:
                pending_elicitations.record_publish(SESSION, event)
            assert pending_elicitations.count_for(SESSION) == 0
        finally:
            waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)
            pending_elicitations.resolve(SESSION, elicitation_id)
    assert keys == ["tool:toolu_10", "tool:toolu_10"]
    assert result["answers"] == [{"question": "Deploy to prod now?", "answer": "Deploy"}]


async def test_a_dismissed_question_tells_the_agent_not_to_ask_again() -> None:
    elicitation_id = workflow_elicitation_id(SESSION, "tool:toolu_dismiss")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"entry": _STATUS, "elicitation_id": elicitation_id})

    async with _client(handler) as client:
        answer = asyncio.create_task(_answer_when_asked(elicitation_id, (False, None)))
        result = await _run(
            client,
            "sys_workflow_request_input",
            {"step": "deploy", "questions": [_QUESTION]},
            call_id="toolu_dismiss",
            publish_event=lambda *_: None,
        )
        await answer
    assert result["answered"] is False
    assert "don't ask again this turn" in result["detail"]


def test_a_question_returns_to_its_agent_before_the_native_tool_relay_gives_up() -> None:
    """The relay can't pause its budget for a wait past the server hop, so the call ends first."""
    from omnigent.harnesses.claude_native import bridge

    assert journal_tools._ANSWER_WAIT_IN_TURN_S < bridge._TOOL_CALL_TIMEOUT_S


def _late_question_server(
    elicitation_id: str,
    messages: list[dict[str, object]],
    asked: list[dict[str, object]] | None = None,
):
    """Takes the question, recording each time it's raised and the messages sent to the session."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/sessions/{SESSION}/events":
            messages.append(json.loads(request.content))
            return httpx.Response(200, json={"queued": True})
        if asked is not None:
            asked.append(json.loads(request.content))
        return httpx.Response(200, json={"entry": _STATUS, "elicitation_id": elicitation_id})

    return handler


_ANSWERED_NOTICE = (
    '[System: the user answered your question on task "deploy"]\n'
    "Q: Deploy to prod now?\n"
    "A: Deploy\n"
    "Write the task's next status: started to resume it, done for an approval given, "
    "failed for one refused. Then go on with the plan."
)


@pytest.mark.parametrize(
    ("action", "content", "notice", "reconnect"),
    [
        ("accept", {"Deploy to prod now?": "Deploy"}, _ANSWERED_NOTICE, False),
        (
            "decline",
            None,
            '[System: the user dismissed your question on task "deploy"]\n'
            "The task stays waiting for them; don't ask again unless they bring it up.",
            False,
        ),
        ("accept", {"Deploy to prod now?": "Deploy"}, _ANSWERED_NOTICE, True),
    ],
    ids=["answered", "dismissed", "answered_after_server_reconnect"],
)
async def test_an_answer_after_the_call_returned_reaches_the_session_as_a_message(
    monkeypatch: pytest.MonkeyPatch,
    action: str,
    content: dict | None,
    notice: str,
    reconnect: bool,
) -> None:
    from omnigent.runner import create_runner_app
    from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient
    from tests.runner.helpers import NullServerClient

    monkeypatch.setattr(journal_tools, "_ANSWER_WAIT_IN_TURN_S", 0.2)
    elicitation_id = workflow_elicitation_id(SESSION, "tool:toolu_late")
    messages: list[dict[str, object]] = []
    asked: list[dict[str, object]] = []
    published: list[dict[str, object]] = []
    harness = _ScriptedHarnessClient([])
    runner = create_runner_app(
        process_manager=cast(Any, _FakeProcessManager(harness)),
        server_client=cast(Any, NullServerClient()),
    )
    async with (
        _client(_late_question_server(elicitation_id, messages, asked)) as server,
        httpx.AsyncClient(
            transport=httpx.ASGITransport(app=runner), base_url="http://runner"
        ) as runner_client,
    ):
        try:
            result = await _run(
                server,
                "sys_workflow_request_input",
                {"step": "deploy", "questions": [_QUESTION]},
                call_id="toolu_late",
                publish_event=lambda _session, event: published.append(event),
            )
            assert result["answered"] is False
            assert result["still_open"] is True
            # Nothing clears the question, so it stays answerable in the Inbox and chat.
            assert [event["type"] for event in published] == ["session.journal.appended"]
            # No turn waits on it any more, so the session's messages aren't held back.
            assert not pending_approvals.has_pending(SESSION)
            if reconnect:
                pending_approvals.notify_server_reconnect()
                await poll_until_async(lambda: len(asked) == 2, timeout_s=5)
                assert asked[0] == asked[1]
                assert not pending_approvals.has_pending(SESSION)
            # The server forwards the answer to the runner as it does any other.
            response = await runner_client.post(
                f"/v1/sessions/{SESSION}/events",
                json={
                    "type": "approval",
                    "data": {
                        "elicitation_id": elicitation_id,
                        "action": action,
                        **({"content": content} if content else {}),
                    },
                },
            )
            assert response.status_code in (200, 204)
            await poll_until_async(lambda: messages, timeout_s=5)
        finally:
            await journal_tools.cancel_session_questions(SESSION)
    assert messages == [
        {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": notice}]},
        }
    ]
    # A late dismissal doesn't interrupt what the agent is doing by then.
    assert [event["type"] for event in harness.patched_events] == ["approval"]
    assert published[-1] == {
        "type": "response.elicitation_resolved",
        "elicitation_id": elicitation_id,
    }
    assert not journal_tools.answer_arrives_as_message(elicitation_id)
    assert not pending_approvals.has_pending(SESSION)


@pytest.mark.parametrize("moment", ["before_delivery_resumes", "while_the_message_is_held"])
async def test_deleting_the_session_stops_an_answer_on_its_way(
    monkeypatch: pytest.MonkeyPatch, moment: str
) -> None:
    monkeypatch.setattr(journal_tools, "_ANSWER_WAIT_IN_TURN_S", 0.2)
    elicitation_id = workflow_elicitation_id(SESSION, "tool:toolu_deleted")
    posting, released = asyncio.Event(), asyncio.Event()
    delivered: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/v1/sessions/{SESSION}/events":
            # Held, as a policy approval gate on the session's input can hold it.
            posting.set()
            await released.wait()
            delivered.append(request)
            return httpx.Response(200, json={"queued": True})
        return httpx.Response(200, json={"entry": _STATUS, "elicitation_id": elicitation_id})

    async with _client(handler) as server:
        try:
            result = await _run(
                server,
                "sys_workflow_request_input",
                {"step": "deploy", "questions": [_QUESTION]},
                call_id="toolu_deleted",
                publish_event=lambda *_: None,
            )
            assert result["still_open"] is True
            question = journal_tools._answers_by_message[elicitation_id]
            assert pending_approvals.resolve(
                elicitation_id, True, {"Deploy to prod now?": "Deploy"}
            )
            if moment == "before_delivery_resumes":
                while not question.answer.done():
                    await asyncio.sleep(0)
                assert not posting.is_set()
            else:
                await asyncio.wait_for(posting.wait(), timeout=5)
            await asyncio.wait_for(journal_tools.cancel_session_questions(SESSION), timeout=5)
            assert question.delivery.done()
            assert not journal_tools.answer_arrives_as_message(elicitation_id)
        finally:
            released.set()
            await journal_tools.cancel_session_questions(SESSION)
    assert delivered == []


@pytest.mark.parametrize("ending", ["expiry", "session_deleted"])
async def test_a_question_closed_without_an_answer_wakes_nobody(
    monkeypatch: pytest.MonkeyPatch, ending: str
) -> None:
    monkeypatch.setattr(journal_tools, "_ANSWER_WAIT_IN_TURN_S", 0.2)
    if ending == "expiry":
        monkeypatch.setattr(pending_approvals, "_DEFAULT_WAIT_SECONDS", 0.6)
    elicitation_id = workflow_elicitation_id(SESSION, "tool:toolu_unanswered")
    messages: list[dict[str, object]] = []
    published: list[dict[str, object]] = []
    async with _client(_late_question_server(elicitation_id, messages)) as server:
        try:
            result = await _run(
                server,
                "sys_workflow_request_input",
                {"step": "deploy", "questions": [_QUESTION]},
                call_id="toolu_unanswered",
                publish_event=lambda _session, event: published.append(event),
            )
            assert result["still_open"] is True
            if ending == "session_deleted":
                await journal_tools.cancel_session_questions(SESSION)
            await poll_until_async(
                lambda: not journal_tools.answer_arrives_as_message(elicitation_id), timeout_s=5
            )
        finally:
            await journal_tools.cancel_session_questions(SESSION)
    assert messages == []
    assert published[-1] == {
        "type": "response.elicitation_resolved",
        "elicitation_id": elicitation_id,
        "reason": "unanswered",
    }
    assert not pending_approvals.has_pending(SESSION)


# ── Saved workflows ───────────────────────────────────

_WORKFLOW_ID = "0123456789abcdef0123456789abcdef"
_STEPS = [
    {"id": "explore", "name": "Explore the failing tests", "depends_on": []},
    {"id": "fix", "name": "Fix the failing tests", "depends_on": ["explore"]},
]
_WORKFLOW = {
    "id": _WORKFLOW_ID,
    "object": "workflow",
    "name": "Triage flaky tests",
    "description": "Finds and fixes flaky tests. Give it: the test suite.",
    "created_at": 1_762_534_860,
    "updated_at": 1_762_534_860,
    "steps": _STEPS,
}
_SAVE_ARGS = {key: _WORKFLOW[key] for key in ("name", "description", "steps")}
# The session's current plan, as GET …/journal returns it.
_PLAN = {
    **_ENTRY,
    "id": "b" * 32,
    "position": 3,
    "kind": "plan",
    "content": {
        "name": "Triage the upload tests",
        "version": 2,
        "source": "agent",
        "steps": _STEPS,
    },
    "workflow_id": None,
}


def _save_handler(seen: list[httpx.Request], plans: list[dict[str, object]]):
    """Answers the save, the read of the session's plan, and the plan written after it."""

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/workflows":
            return httpx.Response(200, json=_WORKFLOW)
        if request.method == "GET":
            return httpx.Response(200, json={"object": "list", "data": plans, "has_more": False})
        body = json.loads(request.content)
        return httpx.Response(
            200, json={**_PLAN, "position": 4, "workflow_id": body["workflow_id"]}
        )

    return handler


async def test_a_save_posts_the_workflow_as_the_session_and_returns_its_id() -> None:
    seen: list[httpx.Request] = []
    args = {**_SAVE_ARGS, "name": " Triage  flaky tests "}
    async with _client(_save_handler(seen, [])) as client:
        result = await _run(client, "sys_workflow_save", args)
    assert result == {"id": _WORKFLOW_ID, "name": "Triage flaky tests"}
    request = seen[0]
    assert (request.method, request.url.path) == ("POST", "/v1/workflows")
    # The server refuses a save from a session shared with edit access, so the call names it.
    assert request.headers[ORCHESTRATION_SOURCE_HEADER] == SESSION
    body = json.loads(request.content)
    assert body["name"] == "Triage flaky tests"
    assert [step["id"] for step in body["steps"]] == ["explore", "fix"]


@pytest.mark.parametrize(
    "failure", ["invalid-json", "missing-content", "invalid-content", "disconnect"]
)
async def test_a_saved_workflow_still_succeeds_when_the_journal_followup_fails(
    failure: str,
) -> None:
    saved = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/workflows":
            saved.append(json.loads(request.content))
            return httpx.Response(200, json=_WORKFLOW)
        if failure == "disconnect":
            raise httpx.ReadError("journal disconnected", request=request)
        if failure == "invalid-json":
            return httpx.Response(200, text="unreadable")
        plan: dict[str, object] = {"workflow_id": None}
        if failure == "invalid-content":
            plan["content"] = []
        return httpx.Response(200, json={"data": [plan]})

    async with _client(handler) as client:
        result = await _run(client, "sys_workflow_save", _SAVE_ARGS)
    assert result == {"id": _WORKFLOW_ID, "name": "Triage flaky tests"}
    assert len(saved) == 1


@pytest.mark.parametrize("saved_steps", [_STEPS, list(reversed(_STEPS))])
async def test_a_save_names_the_sessions_plan_after_the_new_workflow(
    saved_steps: list[dict[str, object]],
) -> None:
    seen: list[httpx.Request] = []
    published: list[dict[str, object]] = []
    async with _client(_save_handler(seen, [_PLAN])) as client:
        result = await _run(
            client,
            "sys_workflow_save",
            {**_SAVE_ARGS, "steps": saved_steps},
            call_id="call_1",
            publish_event=lambda session, event: published.append(event),
        )
    assert result == {"id": _WORKFLOW_ID, "name": "Triage flaky tests"}
    save, read, write = seen
    assert save.url.path == "/v1/workflows"
    assert (read.method, read.url.path) == ("GET", f"/v1/sessions/{SESSION}/journal")
    assert dict(read.url.params) == {"order": "desc", "limit": "1", "kind": "plan"}
    assert (write.method, write.url.path) == ("POST", f"/v1/sessions/{SESSION}/journal")
    body = json.loads(write.content)
    # The same plan, now naming the workflow, so the Tasks tab can show it.
    assert body["kind"] == "plan"
    assert body["workflow_id"] == _WORKFLOW_ID
    assert body["start_run"] is False
    assert body["content"]["name"] == "Triage the upload tests"
    assert [step["id"] for step in body["content"]["steps"]] == ["explore", "fix"]
    assert body["idempotency_key"] == "tool:call_1:plan"
    assert [event["entry"]["workflow_id"] for event in published] == [_WORKFLOW_ID]


@pytest.mark.parametrize(
    "plans",
    [
        [],
        [{**_PLAN, "workflow_id": _WORKFLOW_ID}],
        [
            {
                **_PLAN,
                "content": {
                    **_PLAN["content"],
                    "steps": [{"id": "document", "name": "Write the guide", "depends_on": []}],
                },
            }
        ],
        [
            {
                **_PLAN,
                "content": {
                    **_PLAN["content"],
                    "steps": [{**step, "depends_on": []} for step in _STEPS],
                },
            }
        ],
        [{**_PLAN, "content": {**_PLAN["content"], "source": "workflow"}}],
    ],
    ids=["no-plan", "already-named", "different-steps", "different-dependencies", "drawn-plan"],
)
async def test_a_save_writes_no_plan_when_there_is_none_to_name(
    plans: list[dict[str, object]],
) -> None:
    seen: list[httpx.Request] = []
    async with _client(_save_handler(seen, plans)) as client:
        result = await _run(client, "sys_workflow_save", _SAVE_ARGS)
    assert result == {"id": _WORKFLOW_ID, "name": "Triage flaky tests"}
    assert [(request.method, request.url.path) for request in seen] == [
        ("POST", "/v1/workflows"),
        ("GET", f"/v1/sessions/{SESSION}/journal"),
    ]


async def test_a_save_with_a_taken_name_or_bad_fields_says_so() -> None:
    async with _client(lambda request: httpx.Response(409, json={})) as client:
        taken = await _run(client, "sys_workflow_save", _SAVE_ARGS)
    assert taken["error"] == "name_taken"

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request expected")

    async with _client(handler) as client:
        missing = await _run(client, "sys_workflow_save", {"name": "x", "steps": _STEPS})
        cyclic = await _run(
            client,
            "sys_workflow_save",
            {
                "name": "x",
                "description": "y",
                "steps": [{"id": "a", "name": "A", "depends_on": ["a"]}],
            },
        )
    assert missing["error"].startswith("invalid_workflow: description")
    assert cyclic["error"].startswith("invalid_workflow:")


@pytest.mark.parametrize(
    ("wanted", "requests"),
    [
        (_WORKFLOW_ID, [f"/v1/workflows/{_WORKFLOW_ID}"]),
        (
            "Triage flaky tests",
            ["/v1/workflows?name=Triage+flaky+tests&limit=100", f"/v1/workflows/{_WORKFLOW_ID}"],
        ),
        (
            "triage FLAKY tests",
            [
                "/v1/workflows?name=triage+FLAKY+tests&limit=100",
                "/v1/workflows?limit=100",
                f"/v1/workflows/{_WORKFLOW_ID}",
            ],
        ),
    ],
    ids=["by-id", "by-name", "by-name-in-another-case"],
)
async def test_a_load_finds_the_workflow_and_returns_its_steps(
    wanted: str, requests: list[str]
) -> None:
    seen: list[str] = []
    summary = {key: value for key, value in _WORKFLOW.items() if key != "steps"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url).removeprefix("http://server.test"))
        assert request.headers[ORCHESTRATION_SOURCE_HEADER] == SESSION
        if request.url.path == f"/v1/workflows/{_WORKFLOW_ID}":
            return httpx.Response(200, json=_WORKFLOW)
        exact = request.url.params.get("name") == "Triage flaky tests"
        listed = [summary] if exact or "name" not in request.url.params else []
        return httpx.Response(200, json={"data": listed, "has_more": False})

    async with _client(handler) as client:
        result = await _run(client, "sys_workflow_load", {"workflow": wanted})
    assert result == {key: _WORKFLOW[key] for key in ("id", "name", "description", "steps")}
    assert seen == requests


async def test_a_load_of_an_unknown_name_suggests_the_closest_ones() -> None:
    names = ["Triage flaky tests", "Release notes", "Triage CI failures"]

    def handler(request: httpx.Request) -> httpx.Response:
        if "name" in request.url.params:
            return httpx.Response(200, json={"data": [], "has_more": False})
        data = [{"id": f"{index:032x}", "name": name} for index, name in enumerate(names)]
        return httpx.Response(200, json={"data": data, "has_more": False})

    async with _client(handler) as client:
        result = await _run(client, "sys_workflow_load", {"workflow": "triage flaky test"})
    assert result["error"] == "workflow_not_found"
    assert result["similar"][0] == "Triage flaky tests"
    assert "Release notes" not in result["similar"]


@pytest.mark.parametrize("cursor", ["2" * 32, "name:VHJpYWdlIGZsYWt5IHRlc3Rz"])
async def test_a_list_pages_the_workflows_with_the_start_of_each_description(cursor: str) -> None:
    long = "Finds and fixes flaky tests. " * 20
    pages = {
        None: {
            "data": [
                {"id": "1" * 32, "name": "Release notes", "description": "Drafts\nthe notes."},
                {"id": "2" * 32, "name": "Triage flaky tests", "description": long},
            ],
            "last_id": "2" * 32,
            "has_more": True,
        },
        cursor: {
            "data": [{"id": "3" * 32, "name": "Upgrade a package", "description": "Bumps one."}],
            "last_id": "3" * 32,
            "has_more": False,
        },
    }
    if cursor.startswith("name:"):
        pages[None]["next_cursor"] = cursor
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url).removeprefix("http://server.test"))
        assert request.headers[ORCHESTRATION_SOURCE_HEADER] == SESSION
        return httpx.Response(200, json=pages[request.url.params.get("after")])

    async with _client(handler) as client:
        first = await _run(client, "sys_workflow_list", {"limit": 2})
        rest = await _run(client, "sys_workflow_list", {"cursor": first["next_cursor"]})
    assert seen == [
        "/v1/workflows?limit=2",
        f"/v1/workflows?limit=20&{httpx.QueryParams({'after': cursor})}",
    ]
    assert first["workflows"][0] == {
        "id": "1" * 32,
        "name": "Release notes",
        "description": "Drafts the notes.",
    }
    preview = first["workflows"][1]["description"]
    assert len(preview) <= 200 and preview.endswith("…")
    assert long.startswith(preview.removesuffix("…"))
    assert first["next_cursor"] == cursor
    assert rest == {
        "workflows": [{"id": "3" * 32, "name": "Upgrade a package", "description": "Bumps one."}],
        "next_cursor": None,
    }


async def test_an_older_server_without_workflows_says_so() -> None:
    async with _client(
        lambda request: httpx.Response(404, json={"detail": "Not Found"})
    ) as client:
        save = await _run(client, "sys_workflow_save", _SAVE_ARGS)
        load = await _run(client, "sys_workflow_load", {"workflow": "Triage flaky tests"})
        listed = await _run(client, "sys_workflow_list", {})
    assert save["error"] == "sys_workflow_save: this Omnigent server has no saved workflows"
    assert load["error"] == "sys_workflow_load: this Omnigent server has no saved workflows"
    assert listed["error"] == "sys_workflow_list: this Omnigent server has no saved workflows"
