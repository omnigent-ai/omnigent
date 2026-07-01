"""Unit tests for the runner-side canvas tool proxy (#2, #12).

``_execute_canvas_tool`` maps ``set_canvas`` to ``PUT /v1/canvas/{id}`` over
``server_client``. These drive it against an ``httpx.MockTransport`` that
records the request, asserting method/path/body plus the guard errors — locking
the mapping the live agent path exercises.
"""

from __future__ import annotations

import json

import httpx

from omnigent.runner.tool_dispatch import _execute_canvas_tool


def _recording_client(recorder: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(200, json={"object": "canvas", "id": "cnv_1"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://srv")


async def test_set_canvas_puts_to_conversation() -> None:
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    out = await _execute_canvas_tool(
        "set_canvas",
        json.dumps({"title": "Plan", "content": "<h1>Hi</h1>", "content_type": "html"}),
        conversation_id="conv_1",
        server_client=client,
    )
    await client.aclose()
    assert reqs[0].method == "PUT"
    assert reqs[0].url.path == "/v1/canvas/conv_1"
    assert json.loads(reqs[0].content) == {
        "title": "Plan",
        "content": "<h1>Hi</h1>",
        "content_type": "html",
    }
    assert json.loads(out) == {"object": "canvas", "id": "cnv_1"}


async def test_set_canvas_defaults_content_type_html() -> None:
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    await _execute_canvas_tool(
        "set_canvas",
        json.dumps({"title": "T", "content": "# md"}),
        conversation_id="conv_2",
        server_client=client,
    )
    await client.aclose()
    assert json.loads(reqs[0].content)["content_type"] == "html"


async def test_guards() -> None:
    # No server client -> clear error, no crash.
    out = await _execute_canvas_tool("set_canvas", "{}", conversation_id="c", server_client=None)
    assert "requires server access" in out

    # No session id -> error, no request made.
    reqs: list[httpx.Request] = []
    client = _recording_client(reqs)
    out = await _execute_canvas_tool(
        "set_canvas",
        json.dumps({"title": "T", "content": "x"}),
        conversation_id=None,
        server_client=client,
    )
    await client.aclose()
    assert "requires a session id" in out
    assert reqs == []
