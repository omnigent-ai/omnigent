"""Tests for runner-local timer tool dispatch."""

from __future__ import annotations

import asyncio
import json
import logging
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


@pytest.mark.asyncio
async def test_timer_set_rejects_zero_delay_repeating() -> None:
    """
    ``repeat=true`` with ``seconds=0`` is rejected with the same error
    the builtin validator returns, and no wake POST is started.

    Without this guard the firing loop would busy-loop ``sleep(0)`` and
    hammer the sessions endpoint forever.
    """
    recorder = _TimerPostRecorder()
    transport = httpx.MockTransport(recorder)

    async with httpx.AsyncClient(transport=transport, base_url="http://server") as server_client:
        output = await execute_tool(
            tool_name="sys_timer_set",
            arguments=json.dumps({"seconds": 0, "repeat": True}),
            conversation_id="conv_parent",
            server_client=server_client,
        )

    assert json.loads(output) == {"error": "seconds must be > 0 when repeat is true"}
    assert recorder.posts == []


@pytest.mark.asyncio
async def test_timer_delivery_logs_http_error_status(caplog: pytest.LogCaptureFixture) -> None:
    """
    HTTP 4xx/5xx on the wake POST is treated as delivery failure.

    ``httpx`` does not raise on error status codes by default; without an
    explicit check the timer would silently ignore a rejected firing.
    """

    class _ErrorResponder:
        """Mock transport that records the POST and returns HTTP 500."""

        def __init__(self) -> None:
            self.posts: list[dict[str, Any]] = []
            self.post_seen = asyncio.Event()

        async def __call__(self, request: httpx.Request) -> httpx.Response:
            self.posts.append({"url": request.url.path, "method": request.method})
            self.post_seen.set()
            return httpx.Response(500, text="internal error")

    responder = _ErrorResponder()
    transport = httpx.MockTransport(responder)

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.tool_dispatch"):
        async with httpx.AsyncClient(
            transport=transport, base_url="http://server"
        ) as server_client:
            output = await execute_tool(
                tool_name="sys_timer_set",
                arguments=json.dumps({"seconds": 0, "note": "boom"}),
                conversation_id="conv_parent",
                server_client=server_client,
            )
            result = json.loads(output)
            assert result["status"] == "scheduled"
            await asyncio.wait_for(responder.post_seen.wait(), timeout=1.0)
            # Let the one-shot loop finish after the failed POST.
            await asyncio.sleep(0.05)

    assert len(responder.posts) == 1
    assert any(
        "firing persist failed" in record.getMessage()
        and result["timer_id"] in record.getMessage()
        for record in caplog.records
    )


# ── Timers the server keeps ───────────────────────────────────

_SESSION = "conv_parent"
_KEPT_TIMER = {
    "timer_id": "timer_" + "a" * 32,
    "scheduled_task_id": "a" * 32,
    "next_run_at": "2026-11-16T12:10:00+00:00",
}


def _timer_server(
    *, keeps: bool, create: httpx.Response | None = None, delete_status: int = 200
) -> tuple[Any, list[httpx.Request]]:
    """A server that may keep timers, recording each request it gets."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/v1/info":
            return httpx.Response(200, json={"scheduled_task_timers": keeps})
        if request.url.path.startswith(f"/v1/sessions/{_SESSION}/timers"):
            if request.method == "DELETE":
                return httpx.Response(delete_status, json={"status": "cancelled"})
            return create or httpx.Response(200, json=_KEPT_TIMER)
        return httpx.Response(202, json={"queued": True})

    return handler, calls


def _runner_timers() -> dict[str, Any]:
    from omnigent.runner import app as runner_app

    return runner_app._session_timers.get(_SESSION, {})


async def _set_timer(server_client: httpx.AsyncClient, **args: Any) -> dict[str, Any]:
    output = await execute_tool(
        tool_name="sys_timer_set",
        arguments=json.dumps(args),
        conversation_id=_SESSION,
        server_client=server_client,
    )
    return json.loads(output)


@pytest.mark.asyncio
async def test_a_long_timer_is_kept_by_the_server_when_it_can() -> None:
    """A ten-minute check-in outlives the runner: the server keeps it, not a runner loop."""
    from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER

    handler, calls = _timer_server(keeps=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        result = await _set_timer(
            server_client, seconds=600, repeat=True, note="workflow check-in"
        )

    assert result == {
        "timer_id": _KEPT_TIMER["timer_id"],
        "status": "scheduled",
        "seconds": 600.0,
        "repeat": True,
        "note": "workflow check-in",
        "durable": True,
        "next_run_at": _KEPT_TIMER["next_run_at"],
    }
    [create] = [call for call in calls if call.method == "POST"]
    assert create.url.path == f"/v1/sessions/{_SESSION}/timers"
    assert json.loads(create.content) == {
        "seconds": 600.0,
        "repeat": True,
        "note": "workflow check-in",
    }
    assert create.headers[ORCHESTRATION_SOURCE_HEADER] == _SESSION
    assert _runner_timers() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("keeps", "create", "reason"),
    [
        (False, None, "this Omnigent server doesn't keep timers"),
        (
            True,
            httpx.Response(
                400,
                json={"error": {"code": "invalid_input", "message": "A side chat can't keep it"}},
            ),
            "A side chat can't keep it",
        ),
    ],
    ids=["unsupported", "refused"],
)
async def test_a_timer_the_server_does_not_keep_lives_in_the_runner(
    keeps: bool, create: httpx.Response | None, reason: str
) -> None:
    handler, calls = _timer_server(keeps=keeps, create=create)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        result = await _set_timer(server_client, seconds=900, note="check CI")
        try:
            assert result["durable"] is False
            assert result["not_durable_because"] == reason
            assert result["timer_id"] in _runner_timers()
        finally:
            cancelled = await execute_tool(
                tool_name="sys_timer_cancel",
                arguments=json.dumps({"timer_id": result["timer_id"]}),
                conversation_id=_SESSION,
                server_client=server_client,
            )
    assert json.loads(cancelled)["status"] == "cancelled"
    # The runner held it, so cancelling never reached the server.
    assert [call.method for call in calls if "/timers" in call.url.path] == (
        ["POST"] if keeps else []
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "args", [{"seconds": 300, "repeat": True}, {"seconds": 630, "repeat": True}]
)
async def test_a_short_or_off_minute_timer_never_asks_the_server(args: dict[str, Any]) -> None:
    handler, calls = _timer_server(keeps=True)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        result = await _set_timer(server_client, **args)
        try:
            assert result["durable"] is False
            assert "not_durable_because" not in result
        finally:
            await execute_tool(
                tool_name="sys_timer_cancel",
                arguments=json.dumps({"timer_id": result["timer_id"]}),
                conversation_id=_SESSION,
                server_client=server_client,
            )
    assert calls == []


@pytest.mark.asyncio
async def test_a_timer_create_that_may_have_landed_is_not_doubled_in_the_runner() -> None:
    handler, _ = _timer_server(keeps=True, create=httpx.Response(504, text="gateway timeout"))
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        result = await _set_timer(server_client, seconds=600)
    assert "may have kept this timer" in result["error"]
    assert _runner_timers() == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(("delete_status", "status"), [(200, "cancelled"), (404, "not_found")])
async def test_cancelling_a_kept_timer_goes_to_the_server(delete_status: int, status: str) -> None:
    from omnigent.runner.identity import ORCHESTRATION_SOURCE_HEADER

    handler, calls = _timer_server(keeps=True, delete_status=delete_status)
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://server"
    ) as server_client:
        output = await execute_tool(
            tool_name="sys_timer_cancel",
            arguments=json.dumps({"timer_id": _KEPT_TIMER["timer_id"]}),
            conversation_id=_SESSION,
            server_client=server_client,
        )
    assert json.loads(output) == {"timer_id": _KEPT_TIMER["timer_id"], "status": status}
    [delete] = calls
    assert delete.method == "DELETE"
    assert delete.url.path == f"/v1/sessions/{_SESSION}/timers/{_KEPT_TIMER['timer_id']}"
    assert delete.headers[ORCHESTRATION_SOURCE_HEADER] == _SESSION
