"""Tests for runner timer tool dispatch: durable timers on the server, the rest in memory."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
import pytest

from omnigent.runner.tool_dispatch import execute_tool


class _TimerServer:
    """
    ``httpx.MockTransport`` handler standing in for the server's timer and event routes.

    ``requests`` records ``(method, path, json body or None)`` for every call, e.g.
    ``("POST", "/v1/sessions/conv_parent/timers", {"seconds": 300.0, ...})``.
    ``lost_reply_path`` names a path whose requests time out after arriving, e.g.
    ``"/v1/info"``. ``info`` is the ``/v1/info`` body, e.g. ``{}`` for an older server
    that predates ``session_timers``. ``wake_status`` answers an in-memory timer's wake
    POST to ``/events``, and ``woken`` is set when one arrives.
    """

    def __init__(
        self,
        *,
        info: dict[str, Any] | None = None,
        status_code: int = 201,
        reply: dict[str, Any] | None = None,
        lost_reply_path: str | None = None,
        wake_status: int = 202,
    ) -> None:
        self.info = info if info is not None else {"session_timers": True}
        self.status_code = status_code
        self.reply = (
            reply
            if reply is not None
            else {"durable": True, "timer_id": "t1", "status": "scheduled"}
        )
        self.lost_reply_path = lost_reply_path
        self.wake_status = wake_status
        self.requests: list[tuple[str, str, Any]] = []
        self.woken = asyncio.Event()

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if request.url.path == self.lost_reply_path:
            raise httpx.ReadTimeout("timed out", request=request)
        if request.url.path == "/v1/info":
            return httpx.Response(200, json=self.info)
        if request.url.path.endswith("/events"):
            self.woken.set()
            return httpx.Response(self.wake_status, json={"queued": True})
        return httpx.Response(self.status_code, json=self.reply)


async def _call(
    client: httpx.AsyncClient, tool_name: str, arguments: dict[str, Any]
) -> dict[str, Any]:
    output = await execute_tool(
        tool_name=tool_name,
        arguments=json.dumps(arguments),
        conversation_id="conv_parent",
        server_client=client,
    )
    return json.loads(output)


def _client(server: _TimerServer) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(server), base_url="http://server")


async def test_timer_set_saves_a_durable_timer_on_the_server() -> None:
    """A timer of 2 minutes or more is saved by the server, which relays the reply."""
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


@pytest.mark.parametrize(
    ("seconds", "server", "reason", "methods"),
    [
        (60, _TimerServer(), "timers under 2 minutes aren't saved on the server", []),
        (300, _TimerServer(info={}), "this server doesn't save timers", ["GET"]),
        (
            300,
            _TimerServer(info={"session_timers": False}),
            "this server doesn't save timers",
            ["GET"],
        ),
        (
            300,
            _TimerServer(lost_reply_path="/v1/info"),
            "couldn't be reached to save it",
            ["GET"],
        ),
        (
            300,
            _TimerServer(
                status_code=200,
                reply={"durable": False, "reason": "a sub-agent's timers aren't saved"},
            ),
            "a sub-agent's timers aren't saved",
            ["GET", "POST"],
        ),
    ],
    ids=["short", "older-server", "timers-off", "server-unreachable", "sub-agent"],
)
async def test_timer_set_keeps_a_timer_the_server_wont_save_in_the_runner(
    seconds: int, server: _TimerServer, reason: str, methods: list[str]
) -> None:
    """The runner keeps the timer itself and says when it would be lost."""
    async with _client(server) as client:
        result = await _call(client, "sys_timer_set", {"seconds": seconds, "repeat": True})
        assert (result["status"], result["durable"]) == ("scheduled", False)
        assert result["timer_id"].startswith("timer_")
        assert reason in result["warning"]
        assert "lost if the runner stops or restarts" in result["warning"]
        assert [method for method, _path, _body in server.requests] == methods
        # The runner cancels its own timer without asking the server.
        cancelled = await _call(client, "sys_timer_cancel", {"timer_id": result["timer_id"]})
    assert cancelled == {"timer_id": result["timer_id"], "status": "cancelled"}
    assert [method for method, _path, _body in server.requests] == methods


async def test_a_runner_timer_wakes_its_session_with_a_hidden_message() -> None:
    """
    A firing stays a ``role="user"`` message, so it starts or steers the next turn, and
    ``is_meta`` keeps the synthetic ``[System: timer ... fired]`` row out of transcripts.
    """
    server = _TimerServer()
    async with _client(server) as client:
        result = await _call(client, "sys_timer_set", {"seconds": 0, "note": "check build"})
        await asyncio.wait_for(server.woken.wait(), timeout=1.0)
    # A one-shot timer wakes the session exactly once.
    assert server.requests == [
        (
            "POST",
            "/v1/sessions/conv_parent/events",
            {
                "type": "message",
                "data": {
                    "role": "user",
                    "is_meta": True,
                    "content": [
                        {
                            "type": "input_text",
                            "text": f"[System: timer {result['timer_id']} fired]"
                            "\nnote: 'check build'",
                        }
                    ],
                },
            },
        )
    ]


async def test_a_runner_timer_logs_a_wake_the_server_rejects(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``httpx`` doesn't raise on an error status, so the loop must check it."""
    server = _TimerServer(wake_status=500)
    with caplog.at_level(logging.WARNING, logger="omnigent.runner.tool_dispatch"):
        async with _client(server) as client:
            result = await _call(client, "sys_timer_set", {"seconds": 0, "note": "boom"})
            await asyncio.wait_for(server.woken.wait(), timeout=1.0)
            # Let the one-shot loop finish after the failed POST.
            await asyncio.sleep(0.05)
    assert any(
        "firing persist failed" in record.getMessage()
        and result["timer_id"] in record.getMessage()
        for record in caplog.records
    )


@pytest.mark.parametrize(
    "arguments,error",
    [
        ({"seconds": -1}, "seconds must be non-negative"),
        # repeat=true with seconds=0 would fire continuously.
        ({"seconds": 0, "repeat": True}, "seconds must be > 0 when repeat is true"),
    ],
)
async def test_timer_set_rejects_invalid_args_via_shared_validator(
    arguments: dict[str, Any], error: str
) -> None:
    """Invalid arguments get the builtin's message and start nothing anywhere."""
    server = _TimerServer()
    async with _client(server) as client:
        assert await _call(client, "sys_timer_set", arguments) == {"error": error}
    assert server.requests == []


@pytest.mark.parametrize(
    "status_code,maybe_set", [(400, False), (502, True)], ids=["rejected", "gateway"]
)
async def test_timer_set_reports_a_server_error(status_code: int, maybe_set: bool) -> None:
    """A rejected save is an error; a gateway error may follow a save, so the agent is told."""
    server = _TimerServer(status_code=status_code, reply={"detail": "no"})
    async with _client(server) as client:
        result = await _call(client, "sys_timer_set", {"seconds": 300})
    assert result["error"].startswith(f"server returned {status_code}")
    assert ("may still have been set" in result["error"]) is maybe_set


async def test_timer_set_says_a_lost_save_reply_may_have_set_a_timer() -> None:
    """No in-memory timer either: the server may already have saved this one."""
    server = _TimerServer(lost_reply_path="/v1/sessions/conv_parent/timers")
    async with _client(server) as client:
        result = await _call(client, "sys_timer_set", {"seconds": 300})
    assert result == {
        "error": "sys_timer_set failed (timed out); the timer may still have been set"
    }


@pytest.mark.parametrize(
    "status_code,expected",
    [
        (200, {"timer_id": "t1", "status": "cancelled"}),
        (404, {"timer_id": "t1", "status": "not_found"}),
    ],
    ids=["cancelled", "not_found"],
)
async def test_timer_cancel_deletes_a_durable_timer_on_the_server(
    status_code: int, expected: dict[str, str]
) -> None:
    server = _TimerServer(status_code=status_code, reply={"timer_id": "t1", "status": "cancelled"})
    async with _client(server) as client:
        assert await _call(client, "sys_timer_cancel", {"timer_id": "t1"}) == expected
    assert server.requests == [("DELETE", "/v1/sessions/conv_parent/timers/t1", None)]


async def test_timer_cancel_of_an_id_no_timer_can_have_asks_nothing() -> None:
    server = _TimerServer()
    async with _client(server) as client:
        result = await _call(client, "sys_timer_cancel", {"timer_id": "../scheduled-tasks"})
    assert result == {"timer_id": "../scheduled-tasks", "status": "not_found"}
    assert server.requests == []
