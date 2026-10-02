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
    unconsumed_inputs.reset_for_tests()
    yield
    unconsumed_inputs.reset_for_tests()


async def _create_session(client: httpx.AsyncClient, agent_id: str) -> dict[str, Any]:
    resp = await client.post("/v1/sessions", json={"agent_id": agent_id})
    assert resp.status_code == 201, f"session create failed: {resp.status_code} {resp.text}"
    return resp.json()


def _capture_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> list[tuple[str, dict[str, Any]]]:
    published: list[tuple[str, dict[str, Any]]] = []

    def capture_publish(session_id: str, event: dict[str, Any]) -> None:
        published.append((session_id, event))

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        capture_publish,
    )
    return published


def _fake_runner(status: str, *, drain_marker: bool = True) -> httpx.AsyncClient:
    """Ack like a current runner; ``drain_marker=False`` acks like one that predates it."""
    ack: dict[str, Any] = {"status": status, "detail": "test"}
    if status == "buffered" and drain_marker:
        ack["drain_marker"] = True
    return httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: httpx.Response(202, json=ack)),
        base_url="http://runner",
    )


def _bind_runner(monkeypatch: pytest.MonkeyPatch, fake_runner: httpx.AsyncClient) -> None:

    async def get_runner_client(_session_id: str, _runner_router: object) -> httpx.AsyncClient:
        return fake_runner

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._get_runner_client",
        get_runner_client,
    )


async def _post_message(
    client: httpx.AsyncClient, session_id: str, text: str, **data: Any
) -> dict[str, Any]:
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}], **data},
        },
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


async def test_buffered_forward_publishes_delivered_not_consumed(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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


async def test_buffered_ack_without_drain_marker_keeps_consumed_at_post_time(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner that never reports drains must not leave the message pending."""
    published = _capture_stream(monkeypatch)
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    fake_runner = _fake_runner("buffered", drain_marker=False)
    _bind_runner(monkeypatch, fake_runner)
    try:
        ack = await _post_message(client, session["id"], "steer into an older runner")
    finally:
        await fake_runner.aclose()

    types = [ev["type"] for _sid, ev in published]
    assert "session.input.consumed" in types
    assert "session.input.delivered" not in types
    consumed = next(ev for _sid, ev in published if ev["type"] == "session.input.consumed")
    assert consumed["data"]["item_id"] == ack["item_id"]

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.json()["unconsumed_input_ids"] == []


async def test_buffered_meta_message_is_consumed_not_tracked(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hidden context is never shown pending, so it is consumed at once."""
    published = _capture_stream(monkeypatch)
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    fake_runner = _fake_runner("buffered")
    _bind_runner(monkeypatch, fake_runner)
    try:
        ack = await _post_message(client, session["id"], "<skill>hidden</skill>", is_meta=True)
    finally:
        await fake_runner.aclose()

    types = [ev["type"] for _sid, ev in published]
    assert "session.input.delivered" not in types
    consumed = next(ev for _sid, ev in published if ev["type"] == "session.input.consumed")
    assert consumed["data"]["item_id"] == ack["item_id"]
    assert consumed["data"]["data"]["is_meta"] is True

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.json()["unconsumed_input_ids"] == []


async def test_relay_drain_marker_upgrades_delivered_to_consumed(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    db_uri: str,
) -> None:
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
    # The marker arrives twice; the duplicate must not publish a second consumed.
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
    assert consumed[0]["data"]["item_id"] == item_id
    assert consumed[0]["data"]["data"]["role"] == "user"
    assert all(ev["type"] != "session.input.drained" for _sid, ev in published)

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.json()["unconsumed_input_ids"] == []


@pytest.mark.parametrize("status", ["failed", "waiting"])
async def test_non_idle_status_keeps_unconsumed_snapshot(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    """The runner fails before draining a continuation; waiting can repeat mid-buffer."""
    published = _capture_stream(monkeypatch)
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    fake_runner = _fake_runner("buffered")
    _bind_runner(monkeypatch, fake_runner)
    try:
        ack = await _post_message(client, session["id"], f"steer then {status} edge")
    finally:
        await fake_runner.aclose()
    published.clear()

    from omnigent.server.routes._sessions.helpers import _publish_status

    _publish_status(session["id"], status)

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.json()["unconsumed_input_ids"] == [ack["item_id"]]
    assert all(ev["type"] != "session.input.consumed" for _sid, ev in published)

    # Idle with the item still tracked means its drain marker was lost, so
    # the server settles it for every client instead of only forgetting it.
    _publish_status(session["id"], "idle")

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.json()["unconsumed_input_ids"] == []
    consumed = [ev for _sid, ev in published if ev["type"] == "session.input.consumed"]
    assert [ev["data"]["item_id"] for ev in consumed] == [ack["item_id"]]


async def test_drain_marker_racing_ahead_of_record_publishes_consumed(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    published = _capture_stream(monkeypatch)
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])

    def _drain_before_ack(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        unconsumed_inputs.resolve(session["id"], body["persisted_item_id"])
        return httpx.Response(
            202, json={"status": "buffered", "drain_marker": True, "detail": "test"}
        )

    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(_drain_before_ack),
        base_url="http://runner",
    )
    _bind_runner(monkeypatch, fake_runner)
    try:
        ack = await _post_message(client, session["id"], "steer with a racing marker")
    finally:
        await fake_runner.aclose()

    types = [ev["type"] for _sid, ev in published]
    assert "session.input.consumed" in types
    assert "session.input.delivered" not in types
    consumed = next(ev for _sid, ev in published if ev["type"] == "session.input.consumed")
    assert consumed["data"]["item_id"] == ack["item_id"]

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.json()["unconsumed_input_ids"] == []


@pytest.mark.parametrize(
    "ack",
    [
        pytest.param(httpx.Response(202, json="ok"), id="non-object-json"),
        pytest.param(httpx.Response(202, content=b"accepted"), id="not-json"),
    ],
)
async def test_unparseable_forward_ack_reads_as_fresh_turn(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    ack: httpx.Response,
) -> None:
    published = _capture_stream(monkeypatch)
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    fake_runner = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _request: ack),
        base_url="http://runner",
    )
    _bind_runner(monkeypatch, fake_runner)
    try:
        posted = await _post_message(client, session["id"], "unparseable ack")
    finally:
        await fake_runner.aclose()

    types = [ev["type"] for _sid, ev in published]
    assert "session.input.consumed" in types
    assert "session.input.delivered" not in types
    consumed = next(ev for _sid, ev in published if ev["type"] == "session.input.consumed")
    assert consumed["data"]["item_id"] == posted["item_id"]

    snap = await client.get(f"/v1/sessions/{session['id']}")
    assert snap.json()["unconsumed_input_ids"] == []
