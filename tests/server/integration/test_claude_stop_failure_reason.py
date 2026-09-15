"""Claude hook failures retain a harness-neutral classification on the API."""

from __future__ import annotations

import httpx
import pytest

from tests.server.helpers import create_test_agent


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["claude-code-native-ui", "claude-code-native-ui-subagent"])
@pytest.mark.parametrize(
    ("detail", "code"),
    [
        ("Claude Code authentication failed.", "native_turn_error"),
        ("Claude Code reported a rate limit error.", "rate_limit_exceeded"),
    ],
)
async def test_claude_failure_output_is_not_labelled_codex(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    wrapper: str,
    detail: str,
    code: str,
) -> None:
    published: list[dict] = []
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda _session_id, event: published.append(event),
    )
    agent = await create_test_agent(client)
    created = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"], "labels": {"omnigent.wrapper": wrapper}},
    )
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    detail += " Check the Claude terminal for details."
    response = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_status",
            "data": {"status": "failed", "output": detail},
        },
    )
    assert response.status_code == 202, response.text
    failed = [event for event in published if event.get("status") == "failed"]
    assert len(failed) == 1
    assert failed[0]["error"]["code"] == code
    assert failed[0]["error"]["message"] == detail
