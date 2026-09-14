"""A re-posted external item with a ``source_id`` must persist exactly once.

The native transcript forwarders deliver at-least-once: a timed-out POST's
disposition is unknown, so the same item may be re-posted — and leaked
concurrent forwarders tailing one transcript post the same records in
parallel. ``data.source_id`` makes the persist idempotent.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.harnesses.antigravity_native import reader, transcript
from omnigent.harnesses.antigravity_native.stop_hook import record_stop_event
from omnigent.inner.antigravity_native_executor import _content_to_text
from omnigent.runtime import pending_inputs
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _create_session(
    client: httpx.AsyncClient,
    name: str,
    *,
    labels: dict[str, str] | None = None,
) -> str:
    agent = await create_test_agent(client, name=name)
    payload: dict[str, object] = {"agent_id": agent["id"]}
    if labels is not None:
        payload["labels"] = labels
    resp = await client.post("/v1/sessions", json=payload)
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


async def test_transcript_reader_restart_dedupes_items_and_preserves_next_pending_input(
    client: httpx.AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = await _create_session(
        client,
        "agy-transcript-restart",
        labels={"omnigent.wrapper": "antigravity-native-ui"},
    )
    bridge_dir = tmp_path / "bridge"
    conversation_id = "8bb3c819-e505-4812-b0f6-895bd2ec1f98"
    app_dir = bridge_dir / "agy-home" / ".gemini" / "antigravity-cli"
    transcript_path = (
        app_dir
        / "brain"
        / conversation_id
        / ".system_generated"
        / "logs"
        / "transcript_full.jsonl"
    )
    transcript_path.parent.mkdir(parents=True)
    cache = app_dir / "cache" / "last_conversations.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps({"/scratch": conversation_id}))

    def step(index: int, source: str, kind: str, content: str) -> str:
        return (
            json.dumps(
                {
                    "step_index": index,
                    "source": source,
                    "type": kind,
                    "status": "DONE",
                    "created_at": f"2026-09-13T02:00:{index:02d}Z",
                    "content": content,
                }
            )
            + "\n"
        )

    transcript_path.write_text(
        step(0, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>first</USER_REQUEST>")
        + step(1, "MODEL", "PLANNER_RESPONSE", "first answer")
    )
    record_stop_event(bridge_dir, {"conversationId": conversation_id, "fullyIdle": True})
    ticks = 0

    async def sleep(_duration: float) -> None:
        nonlocal ticks
        ticks += 1

    monkeypatch.setattr(reader, "_sleep", sleep)

    async def mirror() -> None:
        nonlocal ticks
        ticks = 0
        await reader._supervise_transcript(
            bridge_dir,
            transcript.TranscriptBinding(conversation_id, transcript_path),
            session_id,
            client=client,
            poll_interval_s=0,
            stop=lambda: ticks >= 4,
            committed_steps_out=None,
        )

    await mirror()
    assert await _message_texts(client, session_id) == ["first", "first answer"]
    pending_id = pending_inputs.record(
        session_id,
        [{"type": "input_text", "text": "second"}],
        created_by="alice@example.com",
    )
    with transcript_path.open("a") as handle:
        handle.write(
            step(2, "USER_EXPLICIT", "USER_INPUT", "<USER_REQUEST>second</USER_REQUEST>")
            + step(3, "MODEL", "PLANNER_RESPONSE", "second answer")
        )
    record_stop_event(bridge_dir, {"conversationId": conversation_id, "fullyIdle": True})
    await mirror()
    assert await _message_texts(client, session_id) == [
        "first",
        "first answer",
        "second",
        "second answer",
    ]
    assert all(
        entry["pending_id"] != pending_id for entry in pending_inputs.snapshot_for(session_id)
    )


async def test_antigravity_native_steering_does_not_consume_queued_web_input(
    client: httpx.AsyncClient, tmp_path: Path
) -> None:
    session_id = await _create_session(
        client,
        "agy-native-steering-pending-input",
        labels={"omnigent.wrapper": "antigravity-native-ui"},
    )
    first_content = [
        {
            "type": "input_image",
            "file_id": "file_first",
            "filename": "first.png",
        },
        {"type": "input_text", "text": "  queued Δ input  "},
    ]
    first_transport_content = [
        {**first_content[0], "image_url": "data:image/png;base64,aGVsbG8="},
        first_content[1],
    ]
    second_content = [
        {
            "type": "input_file",
            "file_id": "file_second",
            "filename": "second.txt",
        }
    ]
    second_transport_content = [
        {**second_content[0], "file_data": "data:text/plain;base64,aGVsbG8="}
    ]
    collision_content = [
        {
            "type": "input_file",
            "file_id": "file_collision",
            "filename": "first.png",
        },
        {"type": "input_text", "text": "collision Δ"},
    ]
    collision_transport_content = [
        {**collision_content[0], "file_data": "data:text/plain;base64,d29ybGQ="},
        collision_content[1],
    ]
    unnamed_content = [
        {
            "type": "input_file",
            "file_id": "file_unnamed",
            "file_data": "data:text/plain;base64,dW5uYW1lZA==",
        }
    ]
    bare_text_content = [
        {"type": "input_text", "text": "  leading "},
        {"type": "text", "text": ""},
        {"type": "text", "text": "\nΔ input  "},
        {"type": "output_text", "text": "ignored"},
    ]
    first_pending_id = pending_inputs.record(
        session_id,
        first_content,
        created_by="web@example.com",
    )
    second_pending_id = pending_inputs.record(
        session_id, second_content, created_by="web@example.com"
    )
    collision_pending_id = pending_inputs.record(
        session_id, collision_content, created_by="web@example.com"
    )
    unnamed_pending_id = pending_inputs.record(
        session_id, unnamed_content, created_by="web@example.com"
    )
    bare_text_pending_id = pending_inputs.record(
        session_id, bare_text_content, created_by="web@example.com"
    )

    await _post_item(
        client,
        session_id,
        text="[Attached: /tmp/other/uploads/other.png]\n  queued Δ input",
        source_id="agy-transcript:cascade:user:1:native",
    )
    steering = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"][0]
    assert steering["content"] == [
        {
            "type": "input_text",
            "text": "[Attached: /tmp/other/uploads/other.png]\n  queued Δ input",
        }
    ]
    assert "created_by" not in steering
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(session_id)] == [
        first_pending_id,
        second_pending_id,
        collision_pending_id,
        unnamed_pending_id,
        bare_text_pending_id,
    ]

    first_text = _content_to_text(first_transport_content, tmp_path / "bridge")
    first = await _post_item(
        client,
        session_id,
        text=first_text,
        source_id="agy-transcript:cascade:user:2:web",
    )
    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    web_input = items[1]
    assert web_input["content"] == [
        first_content[0],
        {"type": "input_text", "text": first_text},
    ]
    assert web_input["created_by"] == "web@example.com"
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(session_id)] == [
        second_pending_id,
        collision_pending_id,
        unnamed_pending_id,
        bare_text_pending_id,
    ]

    duplicate = await _post_item(
        client,
        session_id,
        text=first_text,
        source_id="agy-transcript:cascade:user:2:web",
    )
    assert duplicate["item_id"] == first["item_id"]
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(session_id)] == [
        second_pending_id,
        collision_pending_id,
        unnamed_pending_id,
        bare_text_pending_id,
    ]

    second_text = _content_to_text(second_transport_content, tmp_path / "bridge")
    await _post_item(
        client,
        session_id,
        text=second_text,
        source_id="agy-transcript:cascade:user:3:web",
    )
    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    attachment_only = items[2]
    assert attachment_only["content"] == [
        second_content[0],
        {"type": "input_text", "text": second_text},
    ]
    assert attachment_only["created_by"] == "web@example.com"
    assert [entry["pending_id"] for entry in pending_inputs.snapshot_for(session_id)] == [
        collision_pending_id,
        unnamed_pending_id,
        bare_text_pending_id,
    ]

    collision_text = _content_to_text(collision_transport_content, tmp_path / "bridge")
    assert re.fullmatch(
        r"\[Attached: .*/uploads/first_[0-9a-f]{12}\.png\]\ncollision Δ",
        collision_text,
    )
    await _post_item(
        client,
        session_id,
        text=collision_text,
        source_id="agy-transcript:cascade:user:4:web",
    )
    unnamed_text = _content_to_text(unnamed_content, tmp_path / "bridge")
    assert re.fullmatch(r"\[Attached: .*/uploads/attachment_[0-9a-f]{8}\.txt\]", unnamed_text)
    await _post_item(
        client,
        session_id,
        text=unnamed_text,
        source_id="agy-transcript:cascade:user:5:web",
    )
    bare_text = _content_to_text(bare_text_content, tmp_path / "bridge")
    await _post_item(
        client,
        session_id,
        text=bare_text,
        source_id="agy-transcript:cascade:user:6:web",
    )
    items = (await client.get(f"/v1/sessions/{session_id}/items")).json()["data"]
    assert items[3]["content"] == [
        collision_content[0],
        {"type": "input_text", "text": collision_text},
    ]
    assert items[3]["created_by"] == "web@example.com"
    assert items[4]["content"] == [
        unnamed_content[0],
        {"type": "input_text", "text": unnamed_text},
    ]
    assert items[4]["created_by"] == "web@example.com"
    assert items[5]["content"] == [{"type": "input_text", "text": bare_text}]
    assert items[5]["created_by"] == "web@example.com"
    assert pending_inputs.snapshot_for(session_id) == []


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


async def test_client_cannot_smuggle_a_stable_id() -> None:
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
