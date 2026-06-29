"""Unit tests for the scheduler fire callback (B2).

``build_inprocess_fire`` turns a fired loop schedule into a user message POSTed
in-process to ``/v1/sessions/{id}/events``. These tests drive the callback
against a tiny capture app via the real :class:`httpx.ASGITransport`, asserting
the request shape, attribution header, the monitor/empty no-ops, and that an
offline runner (a >=400 status) is swallowed so the cron loop survives.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.entities.schedule import Schedule
from omnigent.runtime.schedule_dispatch import build_inprocess_fire


def _loop(**over: Any) -> Schedule:
    """A ready-to-fire loop schedule, overridable per test."""
    base: dict[str, Any] = {
        "id": "sch_1",
        "conversation_id": "conv_1",
        "name": "weekly",
        "kind": "loop",
        "prompt": "run the weekly report",
        "enabled": True,
        "status": "idle",
        "created_at": 0,
        "cron": "0 22 * * FRI",
        "created_by_user_id": "alice@example.com",
    }
    base.update(over)
    return Schedule(**base)


def _capture_app(status_code: int = 200) -> tuple[FastAPI, dict[str, Any]]:
    """A minimal app exposing the events route; records the one request it gets."""
    app = FastAPI()
    captured: dict[str, Any] = {}

    @app.post("/v1/sessions/{session_id}/events")
    async def events(session_id: str, request: Request) -> JSONResponse:
        captured["session_id"] = session_id
        captured["headers"] = dict(request.headers)
        captured["body"] = await request.json()
        return JSONResponse({"queued": True}, status_code=status_code)

    return app, captured


async def test_fire_posts_user_message_with_attribution() -> None:
    app, captured = _capture_app(200)
    fire = build_inprocess_fire(app, identity_header="X-Forwarded-Email")

    await fire(_loop())

    assert captured["session_id"] == "conv_1"
    assert captured["body"] == {
        "type": "message",
        "data": {
            "role": "user",
            "content": [{"type": "input_text", "text": "run the weekly report"}],
        },
    }
    # Attributed to the schedule's creator via the trusted identity header.
    assert captured["headers"]["x-forwarded-email"] == "alice@example.com"


async def test_fire_omits_identity_header_without_a_creator() -> None:
    app, captured = _capture_app(200)
    fire = build_inprocess_fire(app, identity_header="X-Forwarded-Email")

    await fire(_loop(created_by_user_id=None))

    assert captured["session_id"] == "conv_1"
    assert "x-forwarded-email" not in captured["headers"]


async def test_fire_omits_header_for_reserved_identity() -> None:
    # The reserved "local" sentinel must NOT be sent as an explicit identity
    # header — header-auth 401s on it, accepting it only as the absent-header
    # fallback. Omitting the header lets that fallback resolve the identity.
    app, captured = _capture_app(200)
    fire = build_inprocess_fire(
        app,
        identity_header="X-Forwarded-Email",
        reserved_identities=frozenset({"local"}),
    )

    await fire(_loop(created_by_user_id="local"))

    assert captured["session_id"] == "conv_1"
    assert "x-forwarded-email" not in captured["headers"]


async def test_fire_is_a_noop_for_monitors_and_empty_prompts() -> None:
    app, captured = _capture_app(200)
    fire = build_inprocess_fire(app, identity_header="X-Forwarded-Email")

    await fire(_loop(kind="monitor", cron=None, command="tail -f log"))
    await fire(_loop(prompt=""))

    # Monitors stream on the host side; an empty prompt has nothing to send.
    assert captured == {}


async def test_fire_swallows_offline_runner() -> None:
    # 503 RUNNER_UNAVAILABLE (and 404/409/410) must not propagate — the loop
    # has to keep its cadence when a session has no live runner.
    app, _ = _capture_app(503)
    fire = build_inprocess_fire(app, identity_header="X-Forwarded-Email")

    await fire(_loop())  # must return without raising


async def test_fire_swallows_unexpected_error_status() -> None:
    app, _ = _capture_app(400)
    fire = build_inprocess_fire(app, identity_header="X-Forwarded-Email")

    await fire(_loop())  # logged, not raised
