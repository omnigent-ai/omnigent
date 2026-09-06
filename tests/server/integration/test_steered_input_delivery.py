"""
Integration tests for the steered-message intermediate state.

A message steered into an already-running turn is persisted at POST time
and parked in the runner's turn buffer — the agent loop has verifiably
NOT consumed it. The server must not claim otherwise: the runner's 202
body distinguishes ``accepted`` (a fresh turn started with the message —
consumed) from ``buffered`` (parked for the active turn — merely
delivered). These tests pin the route/relay contract that backs the
grayed-out "awaiting the agent" bubble:

* a ``buffered`` forward publishes ``session.input.delivered`` (not
  ``session.input.consumed``) and reports the item id in the snapshot's
  ``unconsumed_input_ids``;
* an ``accepted`` forward keeps today's behavior: ``session.input.consumed``
  at POST time, nothing pending in the snapshot;
* the runner's ``session.input.drained`` relay marker upgrades the
  delivered item to the canonical ``session.input.consumed`` (full item
  payload) and clears it from the snapshot;
* a terminal session status clears any still-pending ids, so a lost
  drain marker cannot strand the intermediate state.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.runtime import unconsumed_inputs
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _clean_unconsumed_inputs_index() -> Any:
    """Reset the process-global unconsumed-inputs index between tests."""
    unconsumed_inputs.reset_for_tests()
    yield
    unconsumed_inputs.reset_for_tests()


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> dict[str, Any]:
    """
    Create a bare session and return the response JSON.

    :param client: The test HTTP client.
    :param agent_id: Agent to bind.
    :returns: The ``POST /v1/sessions`` response body.
    """
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 201, f"session create failed: {resp.status_code} {resp.text}"
    return resp.json()


def _capture_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, dict[str, Any]]]:
    """
    Capture every session-stream publish for assertion.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: The list publishes are appended to as ``(session_id, event)``.
    """
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )
    return published


def _fake_runner(status: str) -> httpx.AsyncClient:
    """
    A runner whose ``POST /events`` acknowledges with the given status.

    :param status: The 202 acknowledgment status, ``"accepted"`` (fresh
        turn) or ``"buffered"`` (parked for the active turn).
    :returns: An httpx client backed by a mock transport.
    """
    return httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(202, json={"status": status, "detail": "test"})
        ),
        base_url="http://runner",
    )


def _bind_runner(monkeypatch: pytest.MonkeyPatch, fake_runner: httpx.AsyncClient) -> None:
    """
    Route the session's runner lookups at the fake runner.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param fake_runner: The stand-in runner client.
    """

    async def get_runner_client(_session_id: str, _runner_router: object) -> httpx.AsyncClient:
        return fake_runner

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        get_runner_client,
    )


async def _post_message(client: httpx.AsyncClient, session_id: str, text: str) -> dict[str, Any]:
    """
    POST a plain user message event and return the 202 body.

    :param client: The test HTTP client.
    :param session_id: Target session.
    :param text: Message text.
    :returns: The acknowledgment body carrying ``item_id``.
    """
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


async def test_buffered_forward_publishes_delivered_not_consumed(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A steered (buffered) message is announced as delivered, not consumed.

    Failure here is the original bug: the route published
    ``session.input.consumed`` at POST time even though the runner only
    parked the message for the active turn, so clients rendered the
    steered bubble exactly like a consumed one.
    """
    published = _capture_stream(monkeypatch)
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    fake_runner = _fake_runner("buffered")
    _bind_runner(monkeypatch, fake_runner)
    try:
        ack = await _post_message(client, session["id"], "steer me in")
    finally:
        await fake_runner.aclose()

    types = [ev["type"] for _sid, ev in published]
    assert "session.input.delivered" in types
    assert "session.input.consumed" not in types
    delivered = next(ev for _sid, ev in published if ev["type"] == "session.input.delivered")
    assert delivered["data"]["item_id"] == ack["item_id"]
    assert delivered["data"]["type"] == "message"
    assert delivered["data"]["data"]["role"] == "user"

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.status_code == 200
    assert snap.json()["unconsumed_input_ids"] == [ack["item_id"]]


async def test_accepted_forward_keeps_consumed_at_post_time(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fresh-turn (accepted) message still publishes consumed immediately."""
    published = _capture_stream(monkeypatch)
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    fake_runner = _fake_runner("accepted")
    _bind_runner(monkeypatch, fake_runner)
    try:
        ack = await _post_message(client, session["id"], "start a fresh turn")
    finally:
        await fake_runner.aclose()

    types = [ev["type"] for _sid, ev in published]
    assert "session.input.consumed" in types
    assert "session.input.delivered" not in types
    consumed = next(ev for _sid, ev in published if ev["type"] == "session.input.consumed")
    assert consumed["data"]["item_id"] == ack["item_id"]

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.json()["unconsumed_input_ids"] == []


async def test_relay_drain_marker_upgrades_delivered_to_consumed(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    db_uri: str,
) -> None:
    """
    The runner's drain marker produces the canonical consumed event.

    When the buffered message actually leaves the runner's buffer for a
    turn, the relay must publish ``session.input.consumed`` carrying the
    persisted item's full payload (clients promote the pending bubble on
    it) and drop the id from the snapshot's ``unconsumed_input_ids``.
    """
    published = _capture_stream(monkeypatch)
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    fake_runner = _fake_runner("buffered")
    _bind_runner(monkeypatch, fake_runner)
    try:
        ack = await _post_message(client, session["id"], "steer then drain")
    finally:
        await fake_runner.aclose()
    item_id = ack["item_id"]
    published.clear()

    marker = json.dumps({"type": "session.input.drained", "item_id": item_id})
    sse_body = f"data: {marker}\n\ndata: [DONE]\n\n".encode()
    stream_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=sse_body)),
        base_url="http://runner",
    )
    from omnigent.server.routes._sessions.orchestration import _relay_runner_stream_once

    try:
        await _relay_runner_stream_once(
            session["id"],
            stream_runner,
            SqlAlchemyConversationStore(db_uri),
        )
    finally:
        await stream_runner.aclose()

    consumed = [ev for _sid, ev in published if ev["type"] == "session.input.consumed"]
    assert len(consumed) == 1
    assert consumed[0]["data"]["item_id"] == item_id
    assert consumed[0]["data"]["data"]["role"] == "user"
    # The raw runner-internal marker must never reach clients.
    assert all(ev["type"] != "session.input.drained" for _sid, ev in published)

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.json()["unconsumed_input_ids"] == []


async def test_duplicate_drain_marker_is_ignored(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    db_uri: str,
) -> None:
    """A replayed drain marker publishes consumed exactly once."""
    published = _capture_stream(monkeypatch)
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    fake_runner = _fake_runner("buffered")
    _bind_runner(monkeypatch, fake_runner)
    try:
        ack = await _post_message(client, session["id"], "steer, drain twice")
    finally:
        await fake_runner.aclose()
    published.clear()

    marker = json.dumps({"type": "session.input.drained", "item_id": ack["item_id"]})
    sse_body = f"data: {marker}\n\ndata: {marker}\n\ndata: [DONE]\n\n".encode()
    stream_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=sse_body)),
        base_url="http://runner",
    )
    from omnigent.server.routes._sessions.orchestration import _relay_runner_stream_once

    try:
        await _relay_runner_stream_once(
            session["id"],
            stream_runner,
            SqlAlchemyConversationStore(db_uri),
        )
    finally:
        await stream_runner.aclose()

    consumed = [ev for _sid, ev in published if ev["type"] == "session.input.consumed"]
    assert len(consumed) == 1


async def test_terminal_status_clears_unconsumed_snapshot(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    An idle/failed edge clears pending ids so a lost marker can't stick.

    The runner suppresses ``idle`` while a buffered message exists, so a
    terminal status means no live turn holds one any more — the snapshot
    must stop reporting the intermediate state.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    fake_runner = _fake_runner("buffered")
    _bind_runner(monkeypatch, fake_runner)
    try:
        ack = await _post_message(client, session["id"], "steer then turn ends")
    finally:
        await fake_runner.aclose()

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.json()["unconsumed_input_ids"] == [ack["item_id"]]

    from omnigent.server.routes._sessions.helpers import _publish_status

    _publish_status(session["id"], "idle")

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.json()["unconsumed_input_ids"] == []
