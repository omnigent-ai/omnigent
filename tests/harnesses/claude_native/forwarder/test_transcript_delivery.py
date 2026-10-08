"""Transcript delivery tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from omnigent.harnesses.claude_native.bridge import (
    record_hook_event,
)
from omnigent.harnesses.claude_native.forwarder import (
    forward_claude_transcript_to_session,
)
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_item_request,
    _get_recorded_request,
    _start_recording_server,
)


@pytest.mark.asyncio
async def test_forwarder_posts_visible_transcript_items(tmp_path: Path) -> None:
    """
    The background forwarder reads Claude JSONL and posts Omnigent items.

    This catches the real-Claude failure where a terminal-originated
    prompt/tool/output sequence was written to Claude's transcript
    but no process tailed that transcript into the Omnigent session
    stream.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "user-1",
                        "message": {"role": "user", "content": "read TODO"},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-tool-1",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_read_1",
                                    "name": "Read",
                                    "input": {"file_path": "TODO.md"},
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "tool-result-1",
                        "parentUuid": "assistant-tool-1",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_read_1",
                                    "content": "todo contents",
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "attachment",
                        "uuid": "queued-stop",
                        "attachment": {
                            "type": "queued_command",
                            "prompt": "STOP",
                            "commandMode": "prompt",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-text-1",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "hello from transcript"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "subtype": "local_command",
                        "uuid": "bash-input-1",
                        "content": "<bash-input>pwd</bash-input>",
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "subtype": "local_command",
                        "uuid": "bash-output-1",
                        "content": (
                            "<bash-stdout>/tmp/project</bash-stdout><bash-stderr></bash-stderr>"
                        ),
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
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
        # Collect the eight transcript items. The transcript path publishes no
        # session status (Claude's status file owns the badge); see
        # ``test_forwarder_publishes_no_status_for_assistant_output``.
        requests = [await _get_recorded_item_request(server) for _index in range(8)]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert [request["path"] for request in requests] == ["/v1/sessions/conv_abc/events"] * 8
    assert [request["body"]["type"] for request in requests] == ["external_conversation_item"] * 8
    posted = [request["body"]["data"] for request in requests]
    assert [item["item_type"] for item in posted] == [
        "message",
        "function_call",
        "function_call_output",
        "message",
        "message",
        "message",
        "terminal_command",
        "terminal_command",
    ]
    # Every item carries its server-side idempotency key: a retried or
    # concurrently re-posted record must dedupe on the server.
    assert all(isinstance(item.get("source_id"), str) and item["source_id"] for item in posted)
    assert posted[0]["item_data"] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "read TODO"}],
    }
    assert posted[1]["item_data"]["name"] == "Read"
    assert posted[1]["item_data"]["call_id"] == "toolu_read_1"
    assert posted[2]["item_data"] == {"call_id": "toolu_read_1", "output": "todo contents"}
    assert posted[3]["item_data"] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "STOP"}],
    }
    assert posted[4]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "hello from transcript"}],
    }
    assert posted[5]["item_data"] == {
        "role": "user",
        "content": [{"type": "input_text", "text": "!pwd"}],
    }
    assert posted[6]["item_data"] == {"kind": "input", "input": "pwd"}
    assert posted[7]["item_data"] == {
        "kind": "output",
        "stdout": "/tmp/project",
        "stderr": "",
    }
    assert posted[1]["response_id"] == posted[2]["response_id"]
    assert posted[3]["response_id"] != posted[2]["response_id"]
    assert posted[4]["response_id"] != posted[2]["response_id"]
    assert posted[5]["response_id"] == posted[6]["response_id"] == posted[7]["response_id"]
    assert posted[5]["response_id"] != posted[4]["response_id"]
    assert posted[1]["response_id"].startswith("resp_claude_")


@pytest.mark.asyncio
async def test_forwarder_posts_web_injected_terminal_transcript_items(tmp_path: Path) -> None:
    """
    Web-injected messages still surface only after Claude records them.

    The ``claude-native`` executor no longer owns transcript streaming
    for Omnigent turns. This fails if a leftover pause/cursor path suppresses
    terminal-originated output after a web message was typed into Claude.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "owned by executor"}],
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
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
        # The item posts FIRST: the transcript path publishes no status at all
        # (Claude's status file owns the badge), so nothing precedes it.
        request = await _get_recorded_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert request["path"] == "/v1/sessions/conv_abc/events"
    assert request["body"]["type"] == "external_conversation_item"
    assert request["body"]["data"]["item_type"] == "message"
    assert request["body"]["data"]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "owned by executor"}],
    }
