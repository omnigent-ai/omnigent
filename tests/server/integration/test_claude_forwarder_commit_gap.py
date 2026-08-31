"""Regression coverage for Claude transcript commit-gap retries."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from omnigent import claude_native_forwarder as forwarder
from omnigent.claude_native_bridge import ClaudeTranscriptItem
from omnigent.runtime import pending_inputs
from omnigent.server import app as app_module
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def test_claude_source_retry_is_idempotent_but_distinct_sources_are_preserved(
    client: httpx.AsyncClient,
) -> None:
    """A stale cursor retry dedupes by source identity, not message text."""
    agent = await create_test_agent(client)
    created = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]

    transcript_item = ClaudeTranscriptItem(
        source_id="claude-record-uuid:0:message",
        item_type="message",
        data={
            "role": "assistant",
            "agent": "claude-code",
            "content": [{"type": "output_text", "text": "commit-gap-marker"}],
        },
        response_id="resp_claude_record_uuid",
    )

    # First forwarder process: the server commits, then the process dies before
    # transcript_forwarder.json records source_id/cursor advancement.
    await forwarder._post_external_conversation_item(
        client,
        session_id=session_id,
        item=transcript_item,
    )
    # Restarted process: stale durable state re-reads and reposts the same source.
    await forwarder._post_external_conversation_item(
        client,
        session_id=session_id,
        item=transcript_item,
    )

    response = await client.get(f"/v1/sessions/{session_id}/items")
    assert response.status_code == 200, response.text
    matching = [
        item
        for item in response.json()["data"]
        if item["type"] == "message"
        and any(block.get("text") == "commit-gap-marker" for block in item.get("content", []))
    ]
    assert len(matching) == 1, (
        "one Claude transcript source produced multiple durable items: "
        f"{[item['id'] for item in matching]}"
    )

    # Identical visible content from a distinct transcript record remains a
    # distinct conversation item; text is not the idempotency key.
    await forwarder._post_external_conversation_item(
        client,
        session_id=session_id,
        item=replace(transcript_item, source_id="claude-record-uuid-2:0:message"),
    )
    response = await client.get(f"/v1/sessions/{session_id}/items")
    assert response.status_code == 200, response.text
    matching = [
        item
        for item in response.json()["data"]
        if item["type"] == "message"
        and any(block.get("text") == "commit-gap-marker" for block in item.get("content", []))
    ]
    assert len(matching) == 2
    assert len({item["id"] for item in matching}) == 2


async def test_committed_source_retry_does_not_drain_newer_pending_input(
    client: httpx.AsyncClient,
) -> None:
    """Retrying committed A must leave the newer optimistic input B intact."""
    agent = await create_test_agent(client)
    created = await client.post("/v1/sessions", json={"agent_id": agent["id"]})
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    source_a = ClaudeTranscriptItem(
        source_id="claude-user-a:0:message",
        item_type="message",
        data={
            "role": "user",
            "content": [{"type": "input_text", "text": "message A"}],
        },
        response_id="resp_a",
    )

    await forwarder._post_external_conversation_item(
        client,
        session_id=session_id,
        item=source_a,
    )
    pending_b = pending_inputs.record(
        session_id,
        [
            {"type": "input_image", "file_id": "file_b", "filename": "b.png"},
            {"type": "input_text", "text": "message B"},
        ],
        created_by="bob@example.com",
    )
    try:
        await forwarder._post_external_conversation_item(
            client,
            session_id=session_id,
            item=source_a,
        )

        pending = pending_inputs.snapshot_for(session_id)
        assert [entry["pending_id"] for entry in pending] == [pending_b]
        assert pending[0]["created_by"] == "bob@example.com"
        assert pending[0]["content"][0]["file_id"] == "file_b"
        response = await client.get(f"/v1/sessions/{session_id}/items")
        matching_a = [
            item
            for item in response.json()["data"]
            if item["type"] == "message"
            and any(block.get("text") == "message A" for block in item.get("content", []))
        ]
        assert len(matching_a) == 1
        assert all(block.get("file_id") != "file_b" for block in matching_a[0]["content"])
    finally:
        pending_inputs.reset_for_tests()


async def test_spa_packaged_old_server_keeps_parent_and_subagent_retryable(
    tmp_path: Path,
) -> None:
    """Exercise the bundled-SPA fallback used by old packaged servers."""
    web_ui_dist = tmp_path / "web-ui"
    web_ui_dist.mkdir()
    (web_ui_dist / "index.html").write_text(
        "<!doctype html><div id='root'></div>",
        encoding="utf-8",
    )
    old_app = FastAPI()

    @old_app.post("/v1/sessions/{session_id}/events")
    async def legacy_events(session_id: str) -> dict[str, bool]:
        """Represent the only event route present in the old package."""
        assert session_id
        return {"queued": False}

    old_app.mount(
        "/",
        app_module._SPAStaticFiles(directory=web_ui_dist, html=True),
    )

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "parent.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "spa-parent-source",
                "message": {"role": "assistant", "content": "parent output"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    parent_state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )

    subagents_dir = transcript_path.parent / transcript_path.stem / "subagents"
    subagents_dir.mkdir(parents=True)
    child_path = subagents_dir / "agent-spa-child.jsonl"
    child_path.write_text(
        json.dumps(
            {
                "isSidechain": True,
                "type": "assistant",
                "uuid": "spa-child-source",
                "message": {"role": "assistant", "content": "child output"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    subagent_state = forwarder.SubagentForwardState(
        subagents={
            "spa-child": forwarder.SubagentEntry(
                subagent_id="spa-child",
                child_conversation_id="conv_spa_child",
            )
        }
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=old_app),
        base_url="http://old-package",
    ) as old_client:
        probe = await old_client.post(
            "/v1/sessions/conv_spa_parent/events/source-id-v1",
            json={"type": "external_conversation_item", "data": {"source_id": "probe"}},
        )
        assert probe.status_code == 404
        assert probe.json() == {
            "error": {
                "code": "not_found",
                "message": "Not found",
            }
        }

        parent_result = await forwarder._forward_available_items(
            client=old_client,
            session_id="conv_spa_parent",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=parent_state,
            retry_tracker=forwarder._PostRetryTracker(
                base_delay_s=0.0,
                max_delay_s=0.0,
            ),
            dedupe=forwarder._ForwardDedupeState(),
        )
        subagent_result = await forwarder._forward_available_subagents(
            client=old_client,
            parent_session_id="conv_spa_parent",
            bridge_dir=bridge_dir,
            transcript_path=transcript_path,
            state=subagent_state,
            agent_name="claude-native-ui",
            start_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
            item_retry_tracker=forwarder._PostRetryTracker(
                base_delay_s=0.0,
                max_delay_s=0.0,
            ),
            status_retry_tracker=forwarder._PostRetryTracker(base_delay_s=0.0),
        )

    assert parent_result.byte_offset == 0
    assert parent_result.seen_source_ids == ()
    child_result = subagent_result.subagents["spa-child"]
    assert child_result.byte_offset == 0
    assert child_result.seen_source_ids == ()
