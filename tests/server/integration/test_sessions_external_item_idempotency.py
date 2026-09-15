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

from omnigent.runtime import pending_inputs
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
        },
    )
    assert _build_new_item(body, "resp").stable_id is None
