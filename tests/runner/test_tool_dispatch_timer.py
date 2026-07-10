"""Tests for runner-local timer tool dispatch."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
import pytest

from omnigent.runner.tool_dispatch import execute_tool


class _TimerPostRecorder:
    """
    ``httpx.MockTransport`` handler that records timer wake POSTs.

    The ``posts`` attribute stores dictionaries with ``url``,
    ``method``, ``json``, and ``headers`` keys, e.g.
    ``{"url": "/v1/sessions/...", "method": "POST"}``.
    """

    def __init__(self) -> None:
        """Initialize an empty call log."""
        self.posts: list[dict[str, Any]] = []
        self.post_seen = asyncio.Event()

    async def __call__(
        self,
        request: httpx.Request,
    ) -> httpx.Response:
        """
        Record a timer wake request and return an accepted response.

        :param request: HTTPX request, e.g. POST to
            ``"/v1/sessions/conv_x/events"``.
        :returns: HTTP 202 response matching the session event endpoint.
        """
        self.posts.append(
            {
                "url": request.url.path,
                "method": request.method,
                "json": json.loads(request.content),
                "headers": dict(request.headers),
            }
        )
        self.post_seen.set()
        return httpx.Response(202, json={"queued": True})


@pytest.mark.asyncio
async def test_timer_firing_posts_hidden_meta_message() -> None:
    """
    Timer firings wake the agent but stay hidden from user-facing UI.

    The timer POST must remain a ``role="user"`` message so the
    sessions event path starts or steers the next turn. Marking it
    ``is_meta=True`` is what makes existing web/TUI transcript
    rendering skip the synthetic ``[System: timer ... fired]`` row.
    """
    recorder = _TimerPostRecorder()
    transport = httpx.MockTransport(recorder)

    async with httpx.AsyncClient(transport=transport, base_url="http://server") as server_client:
        output = await execute_tool(
            tool_name="sys_timer_set",
            arguments=json.dumps({"seconds": 0, "note": "check build"}),
            conversation_id="conv_parent",
            server_client=server_client,
        )

        result = json.loads(output)
        assert result["status"] == "scheduled"
        assert isinstance(result["timer_id"], str)

        await asyncio.wait_for(recorder.post_seen.wait(), timeout=1.0)

    # A non-repeating timer should produce exactly one wake POST:
    # zero means the firing never reached AP, more than one means it
    # accidentally behaved like a repeating timer.
    assert len(recorder.posts) == 1
    post = recorder.posts[0]
    assert post["method"] == "POST"
    assert post["url"] == "/v1/sessions/conv_parent/events"
    payload = post["json"]
    assert payload == {
        "type": "message",
        "data": {
            "role": "user",
            "is_meta": True,
            "content": [
                {
                    "type": "input_text",
                    "text": f"[System: timer {result['timer_id']} fired]\nnote: 'check build'",
                }
            ],
        },
    }


@pytest.mark.asyncio
async def test_timer_set_rejects_invalid_args_via_shared_validator() -> None:
    """
    The runner dispatch path validates through the shared
    ``validate_timer_set_args`` helper, so a bad ``seconds`` returns the
    same message the in-process builtin surfaces and starts no timer
    task (no wake POST is ever made).
    """
    recorder = _TimerPostRecorder()
    transport = httpx.MockTransport(recorder)

    async with httpx.AsyncClient(transport=transport, base_url="http://server") as server_client:
        output = await execute_tool(
            tool_name="sys_timer_set",
            arguments=json.dumps({"seconds": -1}),
            conversation_id="conv_parent",
            server_client=server_client,
        )

    assert json.loads(output) == {"error": "seconds must be non-negative"}
    assert recorder.posts == []
