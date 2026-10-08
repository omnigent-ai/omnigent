"""Settlement tests for Claude-native forwarding."""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    ClaudeTranscriptItem,
    prepare_bridge_dir,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata_prefix", [False, True])
async def test_partial_transcript_tail_does_not_promote_pending_settle(
    tmp_path: Path, metadata_prefix: bool
) -> None:
    """An incomplete final record is not quiescence and must not become a scheduled wake."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    prefix = (
        json.dumps(
            {
                "type": "assistant",
                "uuid": "before-tail",
                "message": {"role": "assistant", "content": "Entering the worktree"},
            }
        )
        + "\n"
    )
    tail = (
        json.dumps(
            {
                "type": "assistant",
                "uuid": "final-tail",
                "message": {"role": "assistant", "content": "Entered the worktree"},
            }
        )
        + "\n"
    )
    split_at = len(tail) // 2
    metadata = json.dumps({"type": "progress"}) + "\n" if metadata_prefix else ""
    transcript_path.write_text(prefix + metadata + tail[:split_at], encoding="utf-8")
    byte_offset = len(prefix.encode("utf-8"))
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=1,
        byte_offset=byte_offset,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, byte_offset),
        current_response_id="current-turn",
        seen_source_ids=("before-tail:0:message",),
        settled_response_id="older-turn",
        pending_settled_response_id="current-turn",
    )
    forwarder._write_forward_state(bridge_dir, state)
    posted_items: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """Record conversation items without substituting transcript parsing or file reads."""
        payload = json.loads(request.content)
        if payload["type"] == "external_conversation_item":
            posted_items.append(payload["data"])
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handle_request), base_url="http://test"
    ) as client:
        dedupe = forwarder._ForwardDedupeState()

        async def _poll(
            current_state: forwarder.TranscriptForwardState,
        ) -> forwarder.TranscriptForwardState:
            """Forward one real transcript batch with the shared settle latch."""
            return await forwarder._forward_available_items(
                client=client,
                session_id="conv_partial_tail",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=current_state,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
            )

        waiting = await _poll(state)
        expected_offset = len((prefix + metadata).encode("utf-8"))
        assert waiting == replace(
            state,
            line_cursor=1 + int(metadata_prefix),
            byte_offset=expected_offset,
            cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(
                transcript_path, expected_offset
            ),
        )
        assert dedupe.pending_settled_response_id == "current-turn"
        assert dedupe.settled_response_id == "older-turn"
        assert not posted_items
        with transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(tail[split_at:])
        completed = await _poll(waiting)
        assert completed.byte_offset == transcript_path.stat().st_size
        assert completed.pending_settled_response_id == "current-turn"
        settled = await _poll(completed)
        assert settled.pending_settled_response_id is None
        assert settled.settled_response_id == "current-turn"
        assert forwarder._read_forward_state(bridge_dir) == settled

    assert len(posted_items) == 1
    assert posted_items[0]["item_type"] == "message"
    assert posted_items[0]["source_id"] == "final-tail:0:message"
    assert posted_items[0]["response_id"] == "current-turn"


@pytest.mark.asyncio
async def test_missing_transcript_persists_new_pending_settle(tmp_path: Path) -> None:
    """A Stop received during relocation stays durable without falsely settling its turn."""
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(json.dumps({"type": "progress"}) + "\n", encoding="utf-8")
    byte_offset = transcript_path.stat().st_size
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=1,
        byte_offset=byte_offset,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, byte_offset),
        current_response_id="current-turn",
        settled_response_id="older-turn",
    )
    forwarder._write_forward_state(bridge_dir, state)
    os.replace(transcript_path, tmp_path / "relocated.jsonl")
    dedupe = forwarder._ForwardDedupeState(pending_settled_response_id="current-turn")

    def _unexpected_request(request: httpx.Request) -> httpx.Response:
        """A missing transcript cannot produce conversation item posts."""
        raise AssertionError(f"Unexpected POST to {request.url}")

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_unexpected_request), base_url="http://test"
    ) as client:
        waiting = await forwarder._forward_available_items(
            client=client,
            session_id="conv_missing_stop",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=dedupe,
        )

    assert waiting == replace(state, pending_settled_response_id="current-turn")
    assert forwarder._read_forward_state(bridge_dir) == waiting
    assert dedupe.pending_settled_response_id == "current-turn"
    assert dedupe.settled_response_id == "older-turn"


def test_transcript_forward_state_persists_settled_response_id(tmp_path: Path) -> None:
    """
    The turn-settle latch survives a forwarder restart via the cursor file.

    A restart inside a scheduled-wake gap must still mark the wake; a
    pre-latch state file (no ``settled_response_id`` key) loads as None.
    """
    bridge_dir = prepare_bridge_dir("conv_x", bridge_id="b2", workspace=tmp_path)
    transcript = tmp_path / "session.jsonl"
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript,
        line_cursor=3,
        byte_offset=64,
        current_response_id="resp_a",
        settled_response_id="resp_a",
        pending_settled_response_id="resp_b",
    )
    forwarder._write_forward_state(bridge_dir, state)
    loaded = forwarder._read_forward_state(bridge_dir)
    assert loaded is not None
    assert loaded.settled_response_id == "resp_a"
    assert loaded.pending_settled_response_id == "resp_b"

    raw = json.loads((bridge_dir / forwarder._FORWARDER_STATE_FILE).read_text("utf-8"))
    del raw["settled_response_id"]
    (bridge_dir / forwarder._FORWARDER_STATE_FILE).write_text(json.dumps(raw), "utf-8")
    legacy = forwarder._read_forward_state(bridge_dir)
    assert legacy is not None
    assert legacy.settled_response_id is None


def test_promote_pending_settle_waits_for_turn_quiescence(tmp_path: Path) -> None:
    """
    A pending settle activates only once its turn has no output in flight.

    The turn's final message can surface after its Stop edge — promoting
    while that batch still
    carries the turn's output would mis-read the tail as a scheduled
    wake and split the answer into a phantom new turn.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    dedupe = forwarder._ForwardDedupeState()
    dedupe.pending_settled_response_id = "resp_a"
    tail = ClaudeTranscriptItem(
        source_id="s1:0:message",
        item_type="message",
        data={"role": "assistant", "content": [{"type": "output_text", "text": "tail"}]},
        response_id="resp_a",
    )
    assert (
        forwarder._promote_pending_settle(
            dedupe, [tail], transcript_path=transcript_path, byte_offset=0
        )
        is False
    )
    assert dedupe.settled_response_id is None
    assert dedupe.pending_settled_response_id == "resp_a"

    # A late tool result also defers: it can surface EARLIER than the
    # assistant tail, and promoting on it would mis-mark that tail.
    late_result = ClaudeTranscriptItem(
        source_id="s2:0:function_call_output",
        item_type="function_call_output",
        data={"call_id": "c1", "output": "done"},
        response_id="resp_a",
    )
    assert (
        forwarder._promote_pending_settle(
            dedupe, [late_result], transcript_path=transcript_path, byte_offset=0
        )
        is False
    )
    assert dedupe.pending_settled_response_id == "resp_a"

    # Items for OTHER turns don't defer; a truly quiet batch promotes.
    other = ClaudeTranscriptItem(
        source_id="s3:0:message",
        item_type="message",
        data={"role": "assistant", "content": [{"type": "output_text", "text": "hi"}]},
        response_id="resp_b",
    )
    assert (
        forwarder._promote_pending_settle(
            dedupe, [other], transcript_path=transcript_path, byte_offset=0
        )
        is True
    )
    assert dedupe.settled_response_id == "resp_a"
    assert dedupe.pending_settled_response_id is None

    # Idempotent once promoted.
    assert (
        forwarder._promote_pending_settle(
            dedupe, [], transcript_path=transcript_path, byte_offset=0
        )
        is False
    )


@pytest.mark.asyncio
async def test_scheduled_wake_forwards_marker_under_a_new_turn_id(tmp_path: Path) -> None:
    """
    The full wake pipeline: settle → quiet-poll promote → marked new turn.

    Poll 1 forwards a turn; its Stop edge records the pending settle
    (covered by the status-events test — recorded directly here). Poll 2
    is quiet and promotes the settle, persisting it. Poll 3 sees new
    assistant entries — a cron firing writes no user entry — and must POST
    the scheduled-wake marker ahead of the resumed output, all under a new
    response id.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "iter-one",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Iteration 1: all green."}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    requests: list[dict[str, Any]] = []

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """
        Accept every forwarder POST, recording its payload.

        :param request: Outbound HTTP request from the forwarder.
        :returns: HTTP 202 for the mock Omnigent endpoint.
        """
        payload = json.loads(request.content.decode("utf-8"))
        assert isinstance(payload, dict)
        requests.append(payload)
        return httpx.Response(202, json={})

    transport = httpx.MockTransport(_handle_request)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        dedupe = forwarder._ForwardDedupeState()
        after_turn = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=dedupe,
        )
        turn_one_id = after_turn.current_response_id
        assert turn_one_id is not None

        # The turn ends: the Stop edge records the pending settle.
        dedupe.pending_settled_response_id = turn_one_id
        quiet = await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=after_turn,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=dedupe,
        )
        assert dedupe.settled_response_id == turn_one_id
        assert quiet.settled_response_id == turn_one_id

        # A cron firing appends assistant output with NO user entry.
        with transcript_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "iter-two",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Iteration 2: still green."}],
                        },
                    }
                )
                + "\n"
            )
        requests.clear()
        await forwarder._forward_available_items(
            client=client,
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=quiet,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=dedupe,
        )

    # No status POST: the transcript path publishes none (Claude's status file
    # owns the badge). The wake is observable entirely in the items — a fresh
    # turn id plus the marker ahead of the resumed output.
    assert [request["type"] for request in requests] == ["external_conversation_item"] * len(
        requests
    )
    items = [r["data"] for r in requests if r["type"] == "external_conversation_item"]
    wake_turn_id = items[0]["response_id"]
    assert wake_turn_id != turn_one_id
    assert [item["item_data"]["role"] for item in items] == ["user", "assistant"]
    assert items[0]["item_data"]["content"] == [
        {"type": "input_text", "text": "[System: scheduled prompt fired]"}
    ]
    assert {item["response_id"] for item in items} == {wake_turn_id}
