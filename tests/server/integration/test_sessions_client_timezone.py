"""The server forwards a web send's ``client_timezone`` to the runner.

The web app reports its browser zone with each user message so the agent reads
an unqualified time ("every day at 9:00 AM") in the user's wall clock rather
than the server's or UTC. The zone rides the forwarded turn body only; it is
never part of the persisted message item.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent

_TEXT = "Create an automation that runs every day at 9:00 AM and summarizes my inbox."


def _user_message() -> dict[str, Any]:
    return {"role": "user", "content": [{"type": "input_text", "text": _TEXT}]}


def _stub_runner(
    monkeypatch: pytest.MonkeyPatch, forwarded: list[dict[str, Any]]
) -> httpx.AsyncClient:
    """Accept every forwarded turn, recording each body in ``forwarded``."""

    def accept(request: httpx.Request) -> httpx.Response:
        forwarded.append(json.loads(request.content))
        return httpx.Response(202, json={"queued": True})

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(accept),
        base_url="http://runner",
    )

    async def get_runner_client(*_: Any, **__: Any) -> httpx.AsyncClient:
        return fake_runner

    monkeypatch.setattr("omnigent.server.routes.sessions._get_runner_client", get_runner_client)
    return fake_runner


async def _send(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    event: dict[str, Any],
) -> tuple[httpx.Response, list[dict[str, Any]], list[dict[str, Any]]]:
    """POST ``event`` to a fresh session; return the response, forwarded turns, and items."""
    forwarded: list[dict[str, Any]] = []
    fake_runner = _stub_runner(monkeypatch, forwarded)
    try:
        agent = await create_test_agent(client)
        create = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
        assert create.status_code == 201, create.text
        session_id = create.json()["id"]
        response = await client.post(f"/v1/sessions/{session_id}/events", json=event)
    finally:
        await fake_runner.aclose()
    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    return response, forwarded, items


@pytest.mark.asyncio
async def test_client_timezone_rides_the_forwarded_turn_but_not_the_item(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response, forwarded, items = await _send(
        client,
        monkeypatch,
        {"type": "message", "data": _user_message(), "client_timezone": "America/Los_Angeles"},
    )

    assert response.status_code == 202, response.text
    turns = [turn for turn in forwarded if turn.get("type") == "message"]
    assert [turn["client_timezone"] for turn in turns] == ["America/Los_Angeles"]
    messages = [item for item in items if item["type"] == "message"]
    assert len(messages) == 1
    assert "client_timezone" not in messages[0]


@pytest.mark.asyncio
async def test_unknown_client_timezone_is_dropped_without_failing_the_send(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A zone this host's tz database cannot resolve must not cost the user the message."""
    response, forwarded, items = await _send(
        client,
        monkeypatch,
        {"type": "message", "data": _user_message(), "client_timezone": "Not/A_Timezone"},
    )

    assert response.status_code == 202, response.text
    turns = [turn for turn in forwarded if turn.get("type") == "message"]
    assert len(turns) == 1
    assert "client_timezone" not in turns[0]
    assert [item["content"][0]["text"] for item in items if item["type"] == "message"] == [_TEXT]


@pytest.mark.asyncio
async def test_send_without_a_client_timezone_forwards_none(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI and SDK sends carry no zone; the runner keeps its UTC defaults for them."""
    response, forwarded, _ = await _send(
        client, monkeypatch, {"type": "message", "data": _user_message()}
    )

    assert response.status_code == 202, response.text
    turns = [turn for turn in forwarded if turn.get("type") == "message"]
    assert len(turns) == 1
    assert "client_timezone" not in turns[0]
