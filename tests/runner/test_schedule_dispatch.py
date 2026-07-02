"""Unit tests for the runner-side schedule tool proxy (#6, #12).

``_execute_schedule_tool`` maps each schedule builtin to a ``/v1/schedules``
REST call over ``server_client`` (the runner has no in-process ScheduleStore).
These drive it against an ``httpx.MockTransport`` that records the request,
asserting method/path/body/params per tool plus the guard errors.
"""

from __future__ import annotations

import json

import httpx

from omnigent.runner.tool_dispatch import _execute_schedule_tool


def _recording_client(recorder: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, json={"ok": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://srv")


async def test_create_loop_posts_body_with_default_conversation() -> None:
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    out = await _execute_schedule_tool(
        "create_loop",
        json.dumps({"name": "L", "prompt": "p", "cron": "0 2 * * *"}),
        conversation_id="conv_X",
        server_client=client,
    )
    await client.aclose()
    assert reqs[0].method == "POST"
    assert reqs[0].url.path == "/v1/schedules"
    assert json.loads(reqs[0].content) == {
        "conversation_id": "conv_X",
        "name": "L",
        "kind": "loop",
        "prompt": "p",
        "cron": "0 2 * * *",
    }
    assert json.loads(out) == {"ok": True}


async def test_create_loop_with_agent_posts_global_body() -> None:
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    await _execute_schedule_tool(
        "create_loop",
        json.dumps({"name": "L", "prompt": "p", "cron": "0 2 * * *", "agent": "reporter"}),
        conversation_id="conv_default",
        server_client=client,
    )
    await client.aclose()
    assert reqs[0].method == "POST"
    assert reqs[0].url.path == "/v1/schedules"
    body = json.loads(reqs[0].content)
    # Global loop → carries agent_name, NOT the current session id.
    assert body["agent_name"] == "reporter"
    assert "conversation_id" not in body
    assert body["kind"] == "loop"


async def test_explicit_conversation_id_overrides_default() -> None:
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    await _execute_schedule_tool(
        "create_loop",
        json.dumps(
            {"name": "L", "prompt": "p", "cron": "* * * * *", "conversation_id": "conv_arg"}
        ),
        conversation_id="conv_default",
        server_client=client,
    )
    await client.aclose()
    assert json.loads(reqs[0].content)["conversation_id"] == "conv_arg"


async def test_list_schedules_scopes_to_conversation() -> None:
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    await _execute_schedule_tool(
        "list_schedules", "{}", conversation_id="conv_Z", server_client=client
    )
    await client.aclose()
    assert reqs[0].method == "GET"
    assert reqs[0].url.path == "/v1/schedules"
    assert reqs[0].url.params.get("conversation_id") == "conv_Z"


async def test_delete_schedule_deletes_by_id() -> None:
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    await _execute_schedule_tool(
        "delete_schedule",
        json.dumps({"schedule_id": "sch_1"}),
        conversation_id=None,
        server_client=client,
    )
    await client.aclose()
    assert reqs[0].method == "DELETE"
    assert reqs[0].url.path == "/v1/schedules/sch_1"


async def test_guards() -> None:
    # No server client → clear error, no crash.
    out = await _execute_schedule_tool(
        "list_schedules", "{}", conversation_id="c", server_client=None
    )
    assert "requires server access" in out

    # Conversation-scoped tool with no session → error, no request made.
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    out = await _execute_schedule_tool(
        "create_loop",
        json.dumps({"name": "L", "prompt": "p", "cron": "x"}),
        conversation_id=None,
        server_client=client,
    )
    assert "requires a session id" in out

    # Missing required id.
    out = await _execute_schedule_tool(
        "delete_schedule", "{}", conversation_id="c", server_client=client
    )
    await client.aclose()
    assert "schedule_id" in out
    assert reqs == []  # neither errored call hit the network
