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


class _TimerServer:
    """
    ``httpx.MockTransport`` handler standing in for the server's timer routes.

    ``requests`` records ``(method, path, json body or None)``. ``info`` is the
    ``/v1/info`` body, e.g. ``{}`` for an older server without ``session_timers``.
    ``lost_reply_path`` names a path whose requests time out after arriving.
    """

    def __init__(
        self,
        *,
        info: dict[str, Any] | None = None,
        status_code: int = 201,
        reply: dict[str, Any] | None = None,
        lost_reply_path: str | None = None,
    ) -> None:
        self.info = info if info is not None else {"session_timers": True}
        self.status_code = status_code
        self.reply = reply or {"durable": True, "timer_id": "t1", "status": "scheduled"}
        self.lost_reply_path = lost_reply_path
        self.requests: list[tuple[str, str, Any]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if request.url.path == self.lost_reply_path:
            raise httpx.ReadTimeout("timed out", request=request)
        if request.url.path == "/v1/info":
            return httpx.Response(200, json=self.info)
        return httpx.Response(self.status_code, json=self.reply)


async def _call(client: httpx.AsyncClient, tool_name: str, arguments: dict[str, Any]) -> Any:
    output = await execute_tool(
        tool_name=tool_name,
        arguments=json.dumps(arguments),
        conversation_id="conv_parent",
        server_client=client,
    )
    return json.loads(output)


def _client(server: _TimerServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(server), base_url="http://server")


@pytest.mark.asyncio
async def test_timer_set_saves_a_timer_of_two_minutes_or_more_on_the_server() -> None:
    server = _TimerServer()
    async with _client(server) as client:
        result = await _call(client, "sys_timer_set", {"seconds": 300, "note": "check build"})
    assert result == server.reply
    assert server.requests == [
        ("GET", "/v1/info", None),
        (
            "POST",
            "/v1/sessions/conv_parent/timers",
            {"seconds": 300.0, "repeat": False, "note": "check build"},
        ),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("seconds", "server", "reason", "methods"),
    [
        (60, _TimerServer(), "timers under 2 minutes aren't saved on the server", []),
        (300, _TimerServer(info={}), "this server doesn't save timers", ["GET"]),
        (300, _TimerServer(lost_reply_path="/v1/info"), "couldn't be reached", ["GET"]),
        (
            300,
            _TimerServer(status_code=200, reply={"durable": False, "reason": "a sub-agent's"}),
            "a sub-agent's",
            ["GET", "POST"],
        ),
    ],
    ids=["short", "older-server", "server-unreachable", "sub-agent"],
)
async def test_a_timer_the_server_wont_save_stays_in_the_runner_and_says_so(
    seconds: int, server: _TimerServer, reason: str, methods: list[str]
) -> None:
    async with _client(server) as client:
        result = await _call(client, "sys_timer_set", {"seconds": seconds, "repeat": True})
        assert (result["status"], result["durable"]) == ("scheduled", False)
        assert reason in result["warning"]
        assert "lost if the runner stops or restarts" in result["warning"]
        # The runner cancels its own timer without asking the server.
        cancelled = await _call(client, "sys_timer_cancel", {"timer_id": result["timer_id"]})
    assert cancelled == {"timer_id": result["timer_id"], "status": "cancelled"}
    assert [method for method, _path, _body in server.requests] == methods


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("server", "error"),
    [
        (_TimerServer(status_code=400, reply={"detail": "no"}), "server returned 400"),
        # A gateway can answer 502 after the server saved the timer, as can a lost reply.
        (_TimerServer(status_code=502, reply={"detail": "no"}), "may still have been set"),
        (
            _TimerServer(lost_reply_path="/v1/sessions/conv_parent/timers"),
            "may still have been set",
        ),
    ],
    ids=["rejected", "gateway", "lost-reply"],
)
async def test_a_failed_save_is_reported_and_starts_no_runner_timer(
    server: _TimerServer, error: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.runner import app as runner_app

    registered: list[tuple[Any, ...]] = []
    monkeypatch.setattr(runner_app, "register_timer", lambda *args: registered.append(args))
    async with _client(server) as client:
        result = await _call(client, "sys_timer_set", {"seconds": 300})
    assert error in result["error"]
    assert ("may still have been set" in result["error"]) is (error != "server returned 400")
    # A save that may have landed must not also start a runner timer, or it could fire twice.
    assert registered == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status_code", "status"), [(200, "cancelled"), (404, "not_found")], ids=["found", "gone"]
)
async def test_timer_cancel_deletes_a_saved_timer_on_the_server(
    status_code: int, status: str
) -> None:
    server = _TimerServer(status_code=status_code, reply={"timer_id": "t1", "status": "cancelled"})
    async with _client(server) as client:
        result = await _call(client, "sys_timer_cancel", {"timer_id": "t1"})
    assert result == {"timer_id": "t1", "status": status}
    assert server.requests == [("DELETE", "/v1/sessions/conv_parent/timers/t1", None)]


@pytest.mark.asyncio
async def test_timer_cancel_of_an_id_no_timer_can_have_asks_nothing() -> None:
    server = _TimerServer()
    async with _client(server) as client:
        result = await _call(client, "sys_timer_cancel", {"timer_id": "../scheduled-tasks"})
    assert result == {"timer_id": "../scheduled-tasks", "status": "not_found"}
    assert server.requests == []
