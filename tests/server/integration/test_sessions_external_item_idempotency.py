"""A re-posted external item with a ``source_id`` must persist exactly once.

The native transcript forwarders deliver at-least-once: a timed-out POST's
disposition is unknown, so the same item may be re-posted — and leaked
concurrent forwarders tailing one transcript post the same records in
parallel. ``data.source_id`` makes the persist idempotent.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.runtime import pending_inputs, session_stream
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _create_session(client: httpx.AsyncClient, name: str) -> str:
    agent = await create_test_agent(client, name=name)
    resp = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _post_item(
    client: httpx.AsyncClient,
    session_id: str,
    *,
    text: str,
    source_id: str | None,
    role: str = "user",
) -> dict[str, Any]:
    data: dict[str, Any] = {
        "item_type": "message",
        "item_data": {
            "role": role,
            "content": [{"type": "input_text", "text": text}],
            **({"agent": "worker"} if role == "assistant" else {}),
        },
        "response_id": "resp_claude_echo",
    }
    if source_id is not None:
        data["source_id"] = source_id
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={"type": "external_conversation_item", "data": data},
    )
    assert resp.status_code in (200, 201, 202), resp.text
    return resp.json()


async def _message_texts(client: httpx.AsyncClient, session_id: str) -> list[str]:
    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    return [
        block.get("text", "")
        for item in items
        if item.get("type") == "message"
        for block in item.get("content", [])
    ]


async def test_reposted_item_with_source_id_persists_once(
    client: httpx.AsyncClient,
) -> None:
    session_id = await _create_session(client, "idem-repost")
    first = await _post_item(client, session_id, text="hello once", source_id="rec-1:0:message")
    second = await _post_item(client, session_id, text="hello once", source_id="rec-1:0:message")
    assert first["item_id"] == second["item_id"]
    assert await _message_texts(client, session_id) == ["hello once"]


async def test_distinct_source_ids_persist_separately(
    client: httpx.AsyncClient,
) -> None:
    session_id = await _create_session(client, "idem-distinct")
    await _post_item(client, session_id, text="same text", source_id="rec-1:0:message")
    await _post_item(client, session_id, text="same text", source_id="rec-2:0:message")
    assert await _message_texts(client, session_id) == ["same text", "same text"]


async def test_repost_without_source_id_keeps_legacy_behavior(
    client: httpx.AsyncClient,
) -> None:
    session_id = await _create_session(client, "idem-legacy")
    await _post_item(client, session_id, text="legacy", source_id=None)
    await _post_item(client, session_id, text="legacy", source_id=None)
    assert await _message_texts(client, session_id) == ["legacy", "legacy"]


async def test_duplicate_repost_restores_the_drained_pending_input(
    client: httpx.AsyncClient,
) -> None:
    """A duplicate must not consume the NEXT queued web message's entry.

    The persist path drains the oldest pending input before it knows the
    item is a duplicate; the dedupe result restores that entry to the
    front of the queue so the next genuine message still claims it.
    """
    session_id = await _create_session(client, "idem-pending")
    await _post_item(client, session_id, text="first msg", source_id="rec-1:0:message")

    next_pending = pending_inputs.record(
        session_id,
        [{"type": "input_text", "text": "second msg"}],
        created_by="alice@example.com",
    )
    # Duplicate of the already-persisted first message arrives late.
    await _post_item(client, session_id, text="first msg", source_id="rec-1:0:message")

    snapshot = pending_inputs.snapshot_for(session_id)
    assert [entry["pending_id"] for entry in snapshot] == [next_pending]
    assert await _message_texts(client, session_id) == ["first msg"]


async def test_native_submission_identity_survives_consumption_and_forwarder_retry(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lost POST receipts can be reconciled after the pending entry is consumed."""
    session_id = await _create_session(client, "idem-submission")
    stable_id = "a" * 32
    pending_id = pending_inputs.record(
        session_id, [{"type": "input_text", "text": "web prompt"}], stable_id=stable_id
    )
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(session_stream, "publish", lambda _session, event: events.append(event))

    first = await _post_item(client, session_id, text="web prompt", source_id="native-1")
    assert first["item_id"] != stable_id
    assert pending_inputs.snapshot_for(session_id) == []
    consumed = [event for event in events if event["type"] == "session.input.consumed"]
    assert len(consumed) == 1
    assert consumed[0]["data"]["cleared_pending_id"] == pending_id
    assert consumed[0]["data"]["data"]["client_submission_id"] == stable_id

    next_id = pending_inputs.record(
        session_id, [{"type": "input_text", "text": "next prompt"}], stable_id="b" * 32
    )
    duplicate = await _post_item(client, session_id, text="web prompt", source_id="native-1")
    assert duplicate["item_id"] == first["item_id"]
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(session_id)] == [next_id]
    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    messages = [item for item in items if item["type"] == "message"]
    assert len(messages) == 1
    assert messages[0]["client_submission_id"] == stable_id


async def test_bad_source_id_is_rejected(client: httpx.AsyncClient) -> None:
    session_id = await _create_session(client, "idem-bad")
    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_conversation_item",
            "data": {
                "item_type": "message",
                "item_data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "x"}],
                },
                "response_id": "resp_claude_echo",
                "source_id": "x" * 300,
            },
        },
    )
    assert resp.status_code == 400


def test_client_cannot_smuggle_a_stable_id() -> None:
    """``stable_id`` is internal-only: a client key inside event data is
    dropped by the item builder, never bound onto the entity."""
    from omnigent.server.routes._sessions.helpers import _build_new_item
    from omnigent.server.schemas import SessionEventInput

    body = SessionEventInput(
        type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "x"}],
            "stable_id": "ab" * 16,
            "client_submission_id": "ab" * 16,
        },
    )
    item = _build_new_item(body, "resp")
    assert item.stable_id is None
    assert item.data.model_dump().get("client_submission_id") is None


class _AcceptingRunnerClient:
    """Stub runner client that accepts the forwarded event POST."""

    async def post(self, path: str, *, json: dict[str, Any], **_: Any) -> Any:
        class _Resp:
            status_code = 202
            headers: dict[str, str] = {}
            text = ""

        return _Resp()

    async def get(self, *_: Any, **__: Any) -> Any:
        raise NotImplementedError


def _stub_runner_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.server.routes import sessions as sessions_mod

    async def _stub(*_: Any, **__: Any) -> _AcceptingRunnerClient:
        return _AcceptingRunnerClient()

    async def _noop(*_: Any, **__: Any) -> None:
        return

    monkeypatch.setattr(sessions_mod, "_get_runner_client", _stub)
    monkeypatch.setattr(sessions_mod, "_ensure_runner_relay_ready", _noop)


async def _user_messages(client: httpx.AsyncClient, session_id: str) -> list[dict[str, Any]]:
    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    return [item for item in items if item["type"] == "message" and item.get("role") == "user"]


async def test_direct_message_post_binds_client_submission_id(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The dispatch path binds the POST's validated ``stable_id`` server-side,
    so the committed item (and its consumed event) can confirm an uncertain
    send on SDK-harness sessions."""
    _stub_runner_dispatch(monkeypatch)
    session_id = await _create_session(client, "direct-submission")
    stable_id = "c" * 32
    events: list[dict[str, Any]] = []
    monkeypatch.setattr(session_stream, "publish", lambda _session, event: events.append(event))

    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "confirm me"}],
                "stable_id": stable_id,
            },
        },
    )
    assert resp.status_code in (200, 202), resp.text

    messages = await _user_messages(client, session_id)
    assert len(messages) == 1
    assert messages[0]["client_submission_id"] == stable_id
    assert messages[0]["id"] != stable_id
    consumed = [event for event in events if event["type"] == "session.input.consumed"]
    assert len(consumed) == 1
    assert consumed[0]["data"]["data"]["client_submission_id"] == stable_id


async def test_direct_message_post_ignores_unvalidated_submission_claims(
    client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a well-formed ``stable_id``, no submission identity is bound —
    a ``client_submission_id`` claimed directly in the payload stays dropped."""
    _stub_runner_dispatch(monkeypatch)
    session_id = await _create_session(client, "direct-unvalidated")

    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "no identity"}],
                "stable_id": "NOT-32-HEX",
                "client_submission_id": "d" * 32,
            },
        },
    )
    assert resp.status_code in (200, 202), resp.text

    messages = await _user_messages(client, session_id)
    assert len(messages) == 1
    assert messages[0].get("client_submission_id") is None
