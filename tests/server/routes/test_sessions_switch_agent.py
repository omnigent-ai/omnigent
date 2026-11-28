"""``POST /v1/sessions/{id}/switch-agent`` is reserved and always returns 410.

In-place agent switching was retired (forking into another agent covers it)
and will return with a new request shape; until then the path must answer 410
with the fork guidance, and must not touch the session.
"""

from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from starlette.testclient import TestClient

from omnigent.server.routes.sessions import create_sessions_router


def test_switch_agent_is_reserved_and_returns_410() -> None:
    conversation_store = SimpleNamespace()  # any store access would raise AttributeError
    app = FastAPI()
    app.include_router(
        create_sessions_router(
            conversation_store=conversation_store,  # type: ignore[arg-type]
            agent_store=SimpleNamespace(),  # type: ignore[arg-type]
        ),
        prefix="/v1",
    )

    resp = TestClient(app).post(
        "/v1/sessions/e9f8f58523cec9a57d3bdf93be543e8c/switch-agent",
        json={"agent_id": "52adb39f0c5ea92b5563da5327dac08f"},
    )

    assert resp.status_code == 410, resp.text
    assert "Fork the session into another agent" in resp.json()["detail"]
