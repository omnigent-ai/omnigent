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
    # The current session id is defaulted onto the item when not supplied.
    assert json.loads(reqs[0].content) == {
        "title": "T",
        "source": "manual",
        "body": "b",
        "conversation_id": "conv_1",
    }
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


async def test_guards() -> None:
    # No server client -> clear error, no crash.
    out = await _execute_work_management_tool(
        "list_work_items", "{}", conversation_id="c", server_client=None
    )
    assert "requires server access" in out

    # Missing required id -> error, no request made.
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    out = await _execute_work_management_tool(
        "update_work_item", "{}", conversation_id="c", server_client=client
    )
    await client.aclose()
    assert "work_item_id" in out
    assert reqs == []  # the errored call did not hit the network
