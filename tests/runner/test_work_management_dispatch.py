"""Unit tests for the runner-side work-management tool proxy (#12).

``_execute_work_management_tool`` maps each builtin to a server REST call over
``server_client``. These drive it against an ``httpx.MockTransport`` that
records the request, asserting method/path/body/params per tool plus the
guard errors — locking the mapping the live agent test exercised end to end.
"""

from __future__ import annotations

import json

import httpx

from omnigent.runner.tool_dispatch import _execute_work_management_tool


def _recording_client(recorder: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, json={"ok": True})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://srv")


async def test_create_work_item_posts_body() -> None:
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    out = await _execute_work_management_tool(
        "create_work_item",
        json.dumps({"title": "T", "source": "manual", "body": "b"}),
        conversation_id="conv_1",
        server_client=client,
    )
    await client.aclose()
    assert reqs[0].method == "POST"
    assert reqs[0].url.path == "/v1/work-items"
    assert json.loads(reqs[0].content) == {"title": "T", "source": "manual", "body": "b"}
    assert json.loads(out) == {"ok": True}


async def test_list_work_items_passes_filters_as_query() -> None:
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    await _execute_work_management_tool(
        "list_work_items",
        json.dumps({"status": "new"}),
        conversation_id="conv_1",
        server_client=client,
    )
    await client.aclose()
    assert reqs[0].method == "GET"
    assert reqs[0].url.path == "/v1/work-items"
    assert reqs[0].url.params.get("status") == "new"


async def test_update_work_item_needs_review_maps_to_status() -> None:
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    await _execute_work_management_tool(
        "update_work_item",
        json.dumps({"work_item_id": "wi_1", "needs_review": True}),
        conversation_id=None,
        server_client=client,
    )
    await client.aclose()
    assert reqs[0].method == "PATCH"
    assert reqs[0].url.path == "/v1/work-items/wi_1"
    assert json.loads(reqs[0].content) == {"status": "needs_review"}


async def test_create_loop_defaults_to_current_conversation() -> None:
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    await _execute_work_management_tool(
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


async def test_list_schedules_scopes_to_conversation() -> None:
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    await _execute_work_management_tool(
        "list_schedules", "{}", conversation_id="conv_Z", server_client=client
    )
    await client.aclose()
    assert reqs[0].method == "GET"
    assert reqs[0].url.path == "/v1/schedules"
    assert reqs[0].url.params.get("conversation_id") == "conv_Z"


async def test_delete_schedule_deletes_by_id() -> None:
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    await _execute_work_management_tool(
        "delete_schedule",
        json.dumps({"schedule_id": "sch_1"}),
        conversation_id=None,
        server_client=client,
    )
    await client.aclose()
    assert reqs[0].method == "DELETE"
    assert reqs[0].url.path == "/v1/schedules/sch_1"


async def test_set_canvas_puts_to_conversation() -> None:
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    await _execute_work_management_tool(
        "set_canvas",
        json.dumps({"title": "C", "content": "<h1>x</h1>"}),
        conversation_id="conv_Y",
        server_client=client,
    )
    await client.aclose()
    assert reqs[0].method == "PUT"
    assert reqs[0].url.path == "/v1/canvas/conv_Y"
    assert json.loads(reqs[0].content) == {
        "title": "C",
        "content": "<h1>x</h1>",
        "content_type": "html",
    }


async def test_guards() -> None:
    # No server client → clear error, no crash.
    out = await _execute_work_management_tool(
        "list_work_items", "{}", conversation_id="c", server_client=None
    )
    assert "requires server access" in out

    # Conversation-scoped tool with no session → error, no request made.
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    out = await _execute_work_management_tool(
        "create_loop",
        json.dumps({"name": "L", "prompt": "p", "cron": "x"}),
        conversation_id=None,
        server_client=client,
    )
    assert "requires a session id" in out

    # Missing required id.
    out = await _execute_work_management_tool(
        "update_work_item", "{}", conversation_id="c", server_client=client
    )
    await client.aclose()
    assert "work_item_id" in out
    assert reqs == []  # neither errored call hit the network
