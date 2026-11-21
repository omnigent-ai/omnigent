"""Tests for runner timer tool dispatch: the server saves, fires and cancels timers."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.runner.tool_dispatch import execute_tool


class _TimerServer:
    """
    ``httpx.MockTransport`` handler standing in for the server's timer routes.

    ``requests`` records ``(method, path, json body or None)`` for every call,
    e.g. ``("POST", "/v1/sessions/conv_parent/timers", {"seconds": 5})``.
    ``lost_reply_path`` names a path whose requests time out after arriving, e.g.
    ``"/v1/info"``.
    """

    def __init__(
        self,
        *,
        timers_supported: bool = True,
        status_code: int = 201,
        reply: dict[str, Any] | None = None,
        lost_reply_path: str | None = None,
    ) -> None:
        self.timers_supported = timers_supported
        self.status_code = status_code
        self.reply = reply if reply is not None else {"timer_id": "t1", "status": "scheduled"}
        self.lost_reply_path = lost_reply_path
        self.requests: list[tuple[str, str, Any]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append((request.method, request.url.path, body))
        if request.url.path == self.lost_reply_path:
            raise httpx.ReadTimeout("timed out", request=request)
        if request.url.path == "/v1/info":
            return httpx.Response(200, json={"session_timers": self.timers_supported})
        return httpx.Response(self.status_code, json=self.reply)


async def _call(server: _TimerServer, tool_name: str, arguments: dict[str, Any]) -> Any:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(server), base_url="http://server"
    ) as client:
        output = await execute_tool(
            tool_name=tool_name,
            arguments=json.dumps(arguments),
            conversation_id="conv_parent",
            server_client=client,
        )
    return json.loads(output)


async def test_timer_set_saves_the_timer_on_the_server() -> None:
    """The runner keeps no timer: it saves the validated arguments and relays the reply."""
    reply = {"timer_id": "t1", "status": "scheduled", "fire_at": "2026-11-21T10:00:05+00:00"}
    server = _TimerServer(reply=reply)
    result = await _call(server, "sys_timer_set", {"seconds": 5, "note": "check build"})
    assert result == reply
    assert server.requests == [
        ("GET", "/v1/info", None),
        (
            "POST",
            "/v1/sessions/conv_parent/timers",
            {"seconds": 5.0, "repeat": False, "note": "check build"},
        ),
    ]


async def test_timer_set_refuses_a_server_without_timers() -> None:
    server = _TimerServer(timers_supported=False)
    result = await _call(server, "sys_timer_set", {"seconds": 5})
    assert "does not run timers" in result["error"]
    assert [method for method, _path, _body in server.requests] == ["GET"]


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
    """Invalid arguments get the builtin's message and never reach the server."""
    server = _TimerServer()
    assert await _call(server, "sys_timer_set", arguments) == {"error": error}
    assert server.requests == []


@pytest.mark.parametrize(
    "status_code,maybe_set", [(400, False), (502, True)], ids=["rejected", "gateway"]
)
async def test_timer_set_reports_a_server_error(status_code: int, maybe_set: bool) -> None:
    """A rejected save is an error; a gateway error may follow a save, so the agent is told."""
    server = _TimerServer(status_code=status_code, reply={"detail": "no"})
    result = await _call(server, "sys_timer_set", {"seconds": 5})
    assert result["error"].startswith(f"server returned {status_code}")
    assert ("may still have been set" in result["error"]) is maybe_set


@pytest.mark.parametrize(
    "lost_reply_path,error,methods",
    [
        # Nothing was sent to save yet, so the agent can simply try again.
        (
            "/v1/info",
            "sys_timer_set could not check server support, so no timer was set: timed out",
            ["GET"],
        ),
        # The server may have saved the timer before its reply was lost.
        (
            "/v1/sessions/conv_parent/timers",
            "sys_timer_set failed (timed out); the timer may still have been set",
            ["GET", "POST"],
        ),
    ],
    ids=["support-check", "save"],
)
async def test_timer_set_says_whether_a_lost_reply_may_have_set_a_timer(
    lost_reply_path: str, error: str, methods: list[str]
) -> None:
    server = _TimerServer(lost_reply_path=lost_reply_path)
    assert await _call(server, "sys_timer_set", {"seconds": 5}) == {"error": error}
    assert [method for method, _path, _body in server.requests] == methods


@pytest.mark.parametrize(
    "status_code,expected",
    [
        (200, {"timer_id": "t1", "status": "cancelled"}),
        (404, {"timer_id": "t1", "status": "not_found"}),
    ],
    ids=["cancelled", "not_found"],
)
async def test_timer_cancel_deletes_the_timer_on_the_server(
    status_code: int, expected: dict[str, str]
) -> None:
    server = _TimerServer(status_code=status_code, reply={"timer_id": "t1", "status": "cancelled"})
    assert await _call(server, "sys_timer_cancel", {"timer_id": "t1"}) == expected
    assert server.requests == [("DELETE", "/v1/sessions/conv_parent/timers/t1", None)]


async def test_timer_cancel_of_an_id_no_timer_can_have_asks_nothing() -> None:
    server = _TimerServer()
    result = await _call(server, "sys_timer_cancel", {"timer_id": "../scheduled-tasks"})
    assert result == {"timer_id": "../scheduled-tasks", "status": "not_found"}
    assert server.requests == []
