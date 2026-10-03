"""Blocked-prompt notice tests for Claude-native forwarding (OMNI-10430)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native.bridge import record_hook_event
from omnigent.harnesses.claude_native.forwarder import forward_claude_transcript_to_session
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_request,
    _RecordingHTTPServer,
    _start_recording_server,
)


async def _drain_prompt_block_request(server: _RecordingHTTPServer) -> dict[str, Any]:
    """
    Await the blocked-prompt error-item POST from the forwarder.

    :param server: Recording HTTP server.
    :returns: The ``external_conversation_item`` body whose item is an error.
    """
    while True:
        request = await _get_recorded_request(server)
        body = request["body"]
        data = body.get("data")
        if (
            body.get("type") == "external_conversation_item"
            and isinstance(data, dict)
            and data.get("item_type") == "error"
        ):
            return body


@pytest.mark.asyncio
async def test_forwarder_posts_prompt_block_notice(tmp_path: Path) -> None:
    """
    A recorded blocked ``UserPromptSubmit`` surfaces as an error item.

    A blocked prompt never reaches Claude's transcript, so without this
    notice the web chat sits idle with no turn (OMNI-10430). This fails if
    the hook loop ignores ``prompt_block_reason`` or posts an item the web
    UI cannot render as a notice.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")

    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    # What the evaluate-policy hook records when it blocks the prompt.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "claude-session",
            "omnigent_prompt_block_reason": (
                "Omnigent policy evaluation unavailable. Detail: auth factory returned empty token"
            ),
        },
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        body = await _drain_prompt_block_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    data = body["data"]
    assert data["item_type"] == "error"
    item = data["item_data"]
    assert item["source"] == "harness"
    assert item["code"] == "prompt_blocked_by_policy"
    assert "auth factory returned empty token" in item["message"]
    # Stable idempotency key tied to the hook record, so a retried POST
    # cannot duplicate the notice.
    assert data["source_id"] == "claude-prompt-block:conv_abc:2"
