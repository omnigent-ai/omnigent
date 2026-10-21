"""Transcript cursors tests for Claude-native forwarding."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import pytest

import omnigent.harnesses.claude_native.forwarder as forwarder
from omnigent.harnesses.claude_native.bridge import (
    TranscriptReadResult,
    record_hook_event,
)
from tests.harnesses.claude_native.forwarder._support import (
    _get_recorded_item_request,
    _get_recorded_request,
    _start_recording_server,
    _wait_for_json_state,
)


async def _wait_for_json_file(path: Path, *, timeout_s: float = 5.0) -> dict[str, Any]:
    """
    Wait until a JSON object file exists and can be parsed.

    :param path: JSON file path.
    :param timeout_s: Maximum seconds to wait.
    :returns: Parsed JSON object.
    """
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert isinstance(payload, dict)
            return payload
        await asyncio.sleep(0.01)
    raise AssertionError(f"{path} was not written")


@pytest.mark.asyncio
async def test_forwarder_start_at_end_uses_byte_offset_for_new_lines(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Reattach mode seeds from the last complete record and tails from there.

    This catches the hot-path regression where ``start_at_end=True``
    counted every old transcript line and subsequent polls rescanned
    the whole file. The compatibility line-cursor reader is patched
    to fail so the test proves the new byte-offset path is used.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    old_prefix = "".join(
        json.dumps(
            {
                "type": "user",
                "uuid": f"old-{index}",
                "message": {"role": "user", "content": f"old {index}"},
            }
        )
        + "\n"
        for index in range(100)
    )
    partial_record = (
        '{"type":"assistant","uuid":"new-assistant","message":{"role":"assistant",'
        '"content":[{"type":"text","text":"new only"}]}'
    )
    transcript_path.write_text(old_prefix + partial_record, encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )

    def _fail_line_cursor_reader(*args: object, **kwargs: object) -> None:
        """
        Fail if start-at-end falls back to the full-file compatibility reader.

        :param args: Positional reader arguments.
        :param kwargs: Keyword reader arguments.
        :returns: Never returns.
        """
        del args, kwargs
        raise AssertionError("start_at_end should seed and poll with byte offsets")

    monkeypatch.setattr(
        "omnigent.harnesses.claude_native.forwarder.read_transcript_items_since_with_position",
        _fail_line_cursor_reader,
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=True,
            poll_interval_s=0.01,
        )
    )
    try:
        state = await _wait_for_json_file(bridge_dir / "transcript_forwarder.json")
        assert state["byte_offset"] == len(old_prefix.encode("utf-8"))
        with transcript_path.open("a", encoding="utf-8") as handle:
            handle.write("}\n")
        request = await _get_recorded_item_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert request["body"]["type"] == "external_conversation_item"
    assert request["body"]["data"]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "new only"}],
    }


@pytest.mark.asyncio
async def test_forwarder_migrates_line_cursor_state_to_byte_offset(tmp_path: Path) -> None:
    """
    Old transcript forwarder state gains a byte cursor after one poll.

    Existing users can have ``transcript_forwarder.json`` files that
    only contain ``line_cursor``. The first poll must preserve their
    cursor semantics, forward only new records after that line, and
    persist ``byte_offset`` so later polls avoid full-file rescans.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "old-user",
                "message": {"role": "user", "content": "already forwarded"},
            }
        )
        + "\n"
        + json.dumps(
            {
                "type": "assistant",
                "uuid": "new-assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "after old cursor"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    (bridge_dir / "transcript_forwarder.json").write_text(
        json.dumps(
            {
                "transcript_path": str(transcript_path),
                "line_cursor": 1,
                "current_response_id": None,
                "seen_source_ids": [],
            }
        ),
        encoding="utf-8",
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
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
        request = await _get_recorded_item_request(server)
        state = await _wait_for_json_state(
            bridge_dir / "transcript_forwarder.json",
            lambda payload: payload.get("line_cursor") == 2 and "byte_offset" in payload,
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert request["body"]["type"] == "external_conversation_item"
    assert request["body"]["data"]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "after old cursor"}],
    }
    assert state["line_cursor"] == 2
    assert state["byte_offset"] == transcript_path.stat().st_size


@pytest.mark.asyncio
async def test_forwarder_waits_for_missing_fresh_transcript_without_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    A new conversation does not warn before Claude creates its transcript.

    Claude hooks can advertise ``transcript_path`` before the JSONL file
    exists. The forwarder should keep the fresh zero cursor, stay quiet
    while the file is missing, and forward the first item once Claude
    creates the file.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "transcript_path": str(transcript_path),
        },
    )
    caplog.set_level(logging.WARNING, logger="omnigent.harnesses.claude_native.forwarder")

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_fresh",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        state = await _wait_for_json_state(
            bridge_dir / "transcript_forwarder.json",
            lambda payload: (
                payload.get("byte_offset") == 0 and "cursor_fingerprint" not in payload
            ),
        )
        assert state["line_cursor"] == 0
        assert "cursor invalid" not in caplog.text

        transcript_path.write_text(
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "first-assistant",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "first reply"}],
                    },
                }
            )
            + "\n",
            encoding="utf-8",
        )

        request = await _get_recorded_item_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert request["body"]["type"] == "external_conversation_item"
    assert request["body"]["data"]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "first reply"}],
    }
    assert "cursor invalid" not in caplog.text
    assert "cursor missing fingerprint" not in caplog.text
    assert "cursor fingerprint changed" not in caplog.text


@pytest.mark.asyncio
async def test_measured_prefix_seed_keeps_a_prompt_injected_during_boot(
    tmp_path: Path,
) -> None:
    """
    Regression: a prompt Claude records while booting must still forward.

    Cold resume writes the transcript prefix itself, then launches Claude. The
    forwarder cannot seed until Claude's first hook advertises the transcript
    path — and the executor's ``inject_user_message`` waits on the same boot,
    so the paste routinely lands first. Seeding from a live end-offset then
    puts the user's prompt BEHIND the cursor: visible in the TUI pane, absent
    from the Omnigent DB, silently, for the session's lifetime.

    Passing the prefix length measured before launch makes the skip exactly the
    prefix, so the boot-window records survive however late the seed runs.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    # The synthesized prefix, complete before Claude starts.
    transcript_path.write_text(
        "".join(
            json.dumps({"type": "user", "uuid": f"old{n}", "message": {"role": "user"}}) + "\n"
            for n in range(3)
        ),
        encoding="utf-8",
    )
    prefix_bytes = transcript_path.stat().st_size
    # Claude boots and records the freshly-injected prompt before the forwarder
    # is scheduled to seed.
    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "boot-window-prompt",
                    "message": {"role": "user", "content": "wake up and check the deploy"},
                }
            )
            + "\n"
        )

    state = await forwarder._ensure_state_for_transcript(
        bridge_dir=bridge_dir,
        state=None,
        transcript_path=transcript_path,
        start_at_end=True,
        session_id="conv_boot_window",
        start_at_offset=prefix_bytes,
    )

    # The measured prefix wins over ``start_at_end``: the cursor sits at the
    # prefix boundary, not at EOF, so the prompt is still ahead of it.
    assert state.byte_offset == prefix_bytes
    result = forwarder._read_transcript_items_for_state(state, "claude-native-ui", None)
    forwarded = [
        block.get("text")
        for item in result.items
        for block in (item.data.get("content") or [])
        if isinstance(block, dict)
    ]
    assert "wake up and check the deploy" in forwarded


@pytest.mark.asyncio
async def test_measured_prefix_never_seeks_past_the_transcript_end(tmp_path: Path) -> None:
    """
    A prefix length larger than the file clamps to the end.

    Defensive: the measurement and the seed are separated by Claude's launch,
    so a truncated or replaced transcript would otherwise leave the cursor
    beyond EOF, where every later read looks like a stale-cursor reset.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps({"type": "user", "uuid": "only", "message": {"role": "user"}}) + "\n",
        encoding="utf-8",
    )

    state = await forwarder._ensure_state_for_transcript(
        bridge_dir=bridge_dir,
        state=None,
        transcript_path=transcript_path,
        start_at_end=True,
        session_id="conv_clamp",
        start_at_offset=10**9,
    )

    assert state.byte_offset == transcript_path.stat().st_size


@pytest.mark.asyncio
@pytest.mark.parametrize("start_at_end", [False, True])
@pytest.mark.parametrize("from_disk", [False, True])
@pytest.mark.parametrize("skip_prefix", [False, True])
@pytest.mark.parametrize("destination_delayed", [False, True])
async def test_relocated_transcript_keeps_the_cursor(
    tmp_path: Path,
    start_at_end: bool,
    from_disk: bool,
    skip_prefix: bool,
    destination_delayed: bool,
) -> None:
    """
    A moved transcript is followed from the same cursor, not re-seeded.

    ``EnterWorktree`` moves ``<old-cwd-slug>/<sid>.jsonl`` into the worktree's
    project dir and Claude keeps appending there. The bytes before the cursor
    are unchanged, so the fingerprint still matches at the new path and the
    cursor must carry over: re-seeding at EOF would skip the tool result
    appended after the move, and byte 0 would re-post the whole turn.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    old_path = tmp_path / "projects" / "-repo" / "sid.jsonl"
    new_path = tmp_path / "projects" / "-worktree" / "sid.jsonl"
    old_path.parent.mkdir(parents=True)
    new_path.parent.mkdir(parents=True)
    old_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "before-move",
                "message": {"role": "user", "content": "enter the worktree"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    forwarded_up_to = old_path.stat().st_size if skip_prefix else 0
    state = forwarder.TranscriptForwardState(
        transcript_path=old_path,
        line_cursor=int(skip_prefix),
        byte_offset=forwarded_up_to,
        current_response_id="current-turn",
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(old_path, forwarded_up_to),
        seen_source_ids=("already-posted:0:message",),
        settled_response_id="settled-turn",
        pending_settled_response_id="pending-turn",
    )
    forwarder._write_forward_state(bridge_dir, state)

    if destination_delayed:
        transit_path = old_path.with_name("moving.jsonl")
        os.replace(old_path, transit_path)
        waiting = await forwarder._ensure_state_for_transcript(
            bridge_dir=bridge_dir,
            state=None if from_disk else state,
            transcript_path=new_path,
            start_at_end=start_at_end,
            session_id="conv_moved",
        )
        assert waiting == state
        assert forwarder._read_forward_state(bridge_dir) == state
        os.replace(transit_path, new_path)
    else:
        os.replace(old_path, new_path)
    with new_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                {
                    "type": "user",
                    "uuid": "after-move",
                    "message": {"role": "user", "content": "entered the worktree"},
                }
            )
            + "\n"
        )

    moved = await forwarder._ensure_state_for_transcript(
        bridge_dir=bridge_dir,
        state=None if from_disk else state,
        transcript_path=new_path,
        start_at_end=start_at_end,
        session_id="conv_moved",
    )

    assert moved == replace(state, transcript_path=new_path)
    assert forwarder._read_forward_state(bridge_dir) == moved
    result = forwarder._read_transcript_items_for_state(moved, "claude-native-ui", None)
    assert [item.source_id for item in result.items] == (
        [] if skip_prefix else ["before-move:0:message"]
    ) + ["after-move:0:message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("start_at_end", [False, True])
@pytest.mark.parametrize("from_disk", [False, True])
@pytest.mark.parametrize(
    "change", ["modified-prefix", "different-session", "different-session-empty-cursor"]
)
async def test_unrelated_transcript_does_not_inherit_relocated_cursor(
    tmp_path: Path, start_at_end: bool, from_disk: bool, change: str
) -> None:
    """Changed content or a different session id must retain cold-start semantics."""
    bridge_dir = tmp_path / "bridge"
    old_path = tmp_path / "original" / "sid.jsonl"
    new_path = (
        tmp_path
        / "worktree"
        / ("sid.jsonl" if change == "modified-prefix" else "other-session.jsonl")
    )
    old_path.parent.mkdir()
    new_path.parent.mkdir()
    prefix = (
        json.dumps(
            {
                "type": "user",
                "uuid": "before-move",
                "message": {"role": "user", "content": "enter the worktree"},
            }
        )
        + "\n"
    )
    old_path.write_text(prefix, encoding="utf-8")
    skip_prefix = change != "different-session-empty-cursor"
    byte_offset = old_path.stat().st_size if skip_prefix else 0
    state = forwarder.TranscriptForwardState(
        transcript_path=old_path,
        line_cursor=int(skip_prefix),
        byte_offset=byte_offset,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(old_path, byte_offset),
        current_response_id="current-turn",
        seen_source_ids=("already-posted:0:message",),
        settled_response_id="settled-turn",
        pending_settled_response_id="pending-turn",
    )
    forwarder._write_forward_state(bridge_dir, state)
    new_path.write_text(
        prefix.replace("before-move", "other--move") if change == "modified-prefix" else prefix,
        encoding="utf-8",
    )

    seeded = await forwarder._ensure_state_for_transcript(
        bridge_dir=bridge_dir,
        state=None if from_disk else state,
        transcript_path=new_path,
        start_at_end=start_at_end,
        session_id="conv_different_transcript",
    )

    expected_offset = new_path.stat().st_size if start_at_end else 0
    assert seeded == forwarder.TranscriptForwardState(
        transcript_path=new_path,
        line_cursor=0,
        byte_offset=expected_offset,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(new_path, expected_offset),
    )
    assert forwarder._read_forward_state(bridge_dir) == seeded


@pytest.mark.asyncio
@pytest.mark.parametrize("start_at_end", [False, True])
@pytest.mark.parametrize("from_disk", [False, True])
@pytest.mark.parametrize("move_at", ["read", "post"])
@pytest.mark.parametrize("destination_delayed", [False, True])
async def test_relocation_during_batch_preserves_unread_result_without_reposting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    start_at_end: bool,
    from_disk: bool,
    move_at: str,
    destination_delayed: bool,
) -> None:
    """A move during a read/POST preserves both the old cursor and newly posted ids."""
    bridge_dir = tmp_path / "bridge"
    old_path = tmp_path / "original" / "sid.jsonl"
    new_path = tmp_path / "worktree" / "sid.jsonl"
    old_path.parent.mkdir()
    new_path.parent.mkdir()
    prefix = (
        json.dumps(
            {
                "type": "user",
                "uuid": "user-prompt",
                "message": {"role": "user", "content": "Enter the worktree"},
            }
        )
        + "\n"
    )
    tool_call = (
        json.dumps(
            {
                "type": "assistant",
                "uuid": "assistant-tool-call",
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "enter-worktree-call",
                            "name": "EnterWorktree",
                            "input": {"path": str(new_path.parent)},
                        }
                    ],
                },
            }
        )
        + "\n"
    )
    tool_result = (
        json.dumps(
            {
                "type": "user",
                "uuid": "user-tool-result",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "enter-worktree-call",
                            "content": "Entered worktree after terminal approval",
                        }
                    ],
                },
            }
        )
        + "\n"
    )
    old_path.write_text(prefix + tool_call, encoding="utf-8")
    byte_offset = len(prefix.encode("utf-8"))
    state = forwarder.TranscriptForwardState(
        transcript_path=old_path,
        line_cursor=1,
        byte_offset=byte_offset,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(old_path, byte_offset),
        seen_source_ids=("user-prompt:0:message",),
    )
    initial = forwarder._read_transcript_items_for_state(state, "claude-native-ui", None)
    assert len(initial.items) == 1
    assert initial.items[0].item_type == "function_call"
    response_id = initial.current_response_id
    assert response_id is not None
    state = replace(
        state, settled_response_id="older-turn", pending_settled_response_id=response_id
    )
    moved_during_batch = False
    posted_items: list[dict[str, Any]] = []

    def _relocate() -> None:
        """Move the actual transcript and append the completed tool result once."""
        nonlocal moved_during_batch
        if moved_during_batch:
            return
        os.replace(old_path, new_path)
        with new_path.open("a", encoding="utf-8") as handle:
            handle.write(tool_result)
        moved_during_batch = True

    if move_at == "read":
        read_items = forwarder._read_transcript_items_for_state

        def _read_then_move(
            read_state: forwarder.TranscriptForwardState,
            agent_name: str,
            settled_response_id: str | None,
        ) -> TranscriptReadResult:
            """Relocate after a real read, before its async caller can persist the cursor."""
            result = read_items(read_state, agent_name, settled_response_id)
            _relocate()
            return result

        monkeypatch.setattr(forwarder, "_read_transcript_items_for_state", _read_then_move)

    def _handle_request(request: httpx.Request) -> httpx.Response:
        """Record posts and optionally relocate while a tool-call POST is in flight."""
        payload = json.loads(request.content)
        if payload["type"] == "external_conversation_item":
            posted_items.append(payload["data"])
            if move_at == "post":
                _relocate()
        return httpx.Response(202, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(_handle_request), base_url="http://test"
    ) as client:
        dedupe = forwarder._ForwardDedupeState()
        forwarded = await forwarder._forward_available_items(
            client=client,
            session_id="conv_moved",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=state,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=dedupe,
        )
        assert moved_during_batch
        assert forwarded == replace(
            state,
            current_response_id=response_id,
            seen_source_ids=(*state.seen_source_ids, initial.items[0].source_id),
        )
        assert forwarder._read_forward_state(bridge_dir) == forwarded
        if destination_delayed:
            transit_path = new_path.with_name("moving.jsonl")
            os.replace(new_path, transit_path)
            waiting = await forwarder._ensure_state_for_transcript(
                bridge_dir=bridge_dir,
                state=None if from_disk else forwarded,
                transcript_path=new_path,
                start_at_end=start_at_end,
                session_id="conv_moved",
            )
            assert waiting == forwarded
            if from_disk:
                dedupe = forwarder._ForwardDedupeState()
            quiet = await forwarder._forward_available_items(
                client=client,
                session_id="conv_moved",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=waiting,
                retry_tracker=forwarder._PostRetryTracker(),
                dedupe=dedupe,
            )
            assert quiet == forwarded
            assert forwarder._read_forward_state(bridge_dir) == forwarded
            assert dedupe.pending_settled_response_id == response_id
            assert dedupe.settled_response_id == "older-turn"
            os.replace(transit_path, new_path)
        relocated = await forwarder._ensure_state_for_transcript(
            bridge_dir=bridge_dir,
            state=None if from_disk else forwarded,
            transcript_path=new_path,
            start_at_end=start_at_end,
            session_id="conv_moved",
        )
        assert relocated == replace(forwarded, transcript_path=new_path)
        if from_disk:
            dedupe = forwarder._ForwardDedupeState()
        completed = await forwarder._forward_available_items(
            client=client,
            session_id="conv_moved",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=relocated,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=dedupe,
        )
        assert completed.byte_offset == new_path.stat().st_size
        assert completed.line_cursor == 3
        assert completed.current_response_id == response_id
        assert completed.settled_response_id == "older-turn"
        assert completed.pending_settled_response_id == response_id
        assert forwarder._read_forward_state(bridge_dir) == completed
        await forwarder._forward_available_items(
            client=client,
            session_id="conv_moved",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            state=completed,
            retry_tracker=forwarder._PostRetryTracker(),
            dedupe=dedupe,
        )

    assert [item["item_type"] for item in posted_items] == [
        "function_call",
        "function_call_output",
    ]
    assert [item["source_id"] for item in posted_items] == [
        "assistant-tool-call:0:function_call",
        "user-tool-result:0:function_call_output",
    ]
    assert [item["response_id"] for item in posted_items] == [response_id, response_id]
    assert posted_items[-1]["item_data"] == {
        "call_id": "enter-worktree-call",
        "output": "Entered worktree after terminal approval",
    }


@pytest.mark.asyncio
async def test_forwarder_skips_to_end_on_stale_byte_cursor_state(tmp_path: Path) -> None:
    """
    Stale byte-offset state skips to end of the replaced transcript.

    A transcript path can be replaced or truncated between polls (e.g.
    after Claude auto-compacts). The forwarder skips to the end of the
    new file so existing content is not re-forwarded, then picks up
    newly-appended records on subsequent polls.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    # Existing content that should NOT be re-forwarded after the skip.
    existing_content = (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "compacted summary"}],
                },
            }
        )
        + "\n"
    )
    transcript_path.write_text(existing_content, encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    (bridge_dir / "transcript_forwarder.json").write_text(
        json.dumps(
            {
                "transcript_path": str(transcript_path),
                "line_cursor": 25,
                "byte_offset": 4096,
                "cursor_fingerprint": "stale",
                "current_response_id": "resp_old",
                "seen_source_ids": ["byte-4096:25:message"],
            }
        ),
        encoding="utf-8",
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
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
        expected_offset = len(existing_content.encode("utf-8"))
        await _wait_for_json_state(
            bridge_dir / "transcript_forwarder.json",
            lambda payload: (
                payload.get("byte_offset") == expected_offset
                and isinstance(payload.get("cursor_fingerprint"), str)
            ),
        )
        # Drain any non-item requests (e.g. PATCH external_session_id).
        item_posts = []
        while not server.requests.empty():
            req = server.requests.get_nowait()
            if req.get("body", {}).get("type") == "external_conversation_item":
                item_posts.append(req)
        assert item_posts == [], (
            "Forwarder should NOT have posted existing content after skip-to-end"
        )
        # Append a NEW record that should be forwarded.
        new_record = (
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "new-after-compaction",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "new output"}],
                    },
                }
            )
            + "\n"
        )
        with transcript_path.open("a", encoding="utf-8") as f:
            f.write(new_record)
        # The new record should be forwarded.
        request = await _get_recorded_item_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert request["body"]["data"]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "new output"}],
    }


@pytest.mark.asyncio
async def test_forwarder_skips_to_end_on_out_of_range_byte_cursor_without_fingerprint(
    tmp_path: Path,
) -> None:
    """
    A legacy byte cursor beyond EOF skips to the end of the truncated file.

    Older state files can contain ``byte_offset`` without
    ``cursor_fingerprint``. If the transcript was truncated afterward
    (e.g. compaction), the forwarder skips to the end of the new file
    so existing content is not re-forwarded, then picks up new records.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"
    existing_content = (
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "after truncation"}],
                },
            }
        )
        + "\n"
    )
    transcript_path.write_text(existing_content, encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    (bridge_dir / "transcript_forwarder.json").write_text(
        json.dumps(
            {
                "transcript_path": str(transcript_path),
                "line_cursor": 25,
                "byte_offset": 4096,
            }
        ),
        encoding="utf-8",
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
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
        expected_offset = len(existing_content.encode("utf-8"))
        await _wait_for_json_state(
            bridge_dir / "transcript_forwarder.json",
            lambda payload: (
                payload.get("byte_offset") == expected_offset
                and isinstance(payload.get("cursor_fingerprint"), str)
            ),
        )
        # Drain any non-item requests (e.g. PATCH external_session_id).
        item_posts = []
        while not server.requests.empty():
            req = server.requests.get_nowait()
            if req.get("body", {}).get("type") == "external_conversation_item":
                item_posts.append(req)
        assert item_posts == [], (
            "Forwarder should NOT have posted existing content after skip-to-end"
        )
        # Append a new record — this one should be forwarded.
        new_record = (
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "new-post-truncation",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "new output"}],
                    },
                }
            )
            + "\n"
        )
        with transcript_path.open("a", encoding="utf-8") as f:
            f.write(new_record)
        request = await _get_recorded_item_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert request["body"]["data"]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "new output"}],
    }


@pytest.mark.asyncio
async def test_forwarder_does_not_replay_after_compaction(tmp_path: Path) -> None:
    """
    Regression test: compaction must not cause the forwarder to re-post
    already-forwarded items.

    Simulates the exact bug scenario: the forwarder has a valid cursor at
    the end of the original transcript, then Claude compacts (rewrites the
    file with new content and different UUIDs). The forwarder must skip to
    the end of the compacted file without posting any of its content, then
    forward only records appended after compaction.

    Before the fix, the forwarder would reset to byte 0 and re-post every
    record in the compacted file, causing the web UI to "replay" the entire
    conversation history in real time.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    transcript_path = tmp_path / "session.jsonl"

    # Phase 1: write the "original" transcript and compute its fingerprint.
    original_records = "".join(
        json.dumps(
            {
                "type": "assistant",
                "uuid": f"original-{i}",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"original message {i}"}],
                },
            }
        )
        + "\n"
        for i in range(5)
    )
    transcript_path.write_text(original_records, encoding="utf-8")
    original_end = len(original_records.encode())
    original_fingerprint = forwarder._jsonl_cursor_fingerprint(transcript_path, original_end)

    # Phase 2: simulate compaction — replace the file with a summary that
    # has DIFFERENT UUIDs (as Claude does during auto-compaction).
    compacted_records = "".join(
        json.dumps(
            {
                "type": "assistant",
                "uuid": f"compacted-{i}",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": f"compacted summary {i}"}],
                },
            }
        )
        + "\n"
        for i in range(3)
    )
    transcript_path.write_text(compacted_records, encoding="utf-8")
    compacted_end = len(compacted_records.encode())

    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )

    # State file simulates a forwarder that had successfully forwarded the
    # original transcript up to the end. The fingerprint will NOT match the
    # compacted file — this is what triggers the skip-to-end behavior.
    (bridge_dir / "transcript_forwarder.json").write_text(
        json.dumps(
            {
                "transcript_path": str(transcript_path),
                "line_cursor": 5,
                "byte_offset": original_end,
                "cursor_fingerprint": original_fingerprint,
                "current_response_id": "resp_old_turn",
                "seen_source_ids": [f"original-{i}:0:message" for i in range(5)],
            }
        ),
        encoding="utf-8",
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_compaction_test",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    try:
        # Wait until the forwarder has actually recovered the stale cursor.
        # A fixed sleep is racy on slow CI: if the post-compaction append
        # lands before validation runs, the stale-cursor recovery correctly
        # skips to the then-current end and this test falsely reports that
        # the fresh record was dropped.
        await _wait_for_json_state(
            bridge_dir / "transcript_forwarder.json",
            lambda payload: payload.get("byte_offset") == compacted_end,
        )

        # Drain any non-item requests (e.g. PATCH external_session_id).
        item_posts = []
        while not server.requests.empty():
            req = server.requests.get_nowait()
            if req.get("body", {}).get("type") == "external_conversation_item":
                item_posts.append(req)
        assert item_posts == [], (
            "Forwarder should NOT have posted compacted content. This is the "
            "compaction replay bug — the forwarder re-posted items that "
            "were already in the web UI."
        )

        # Phase 3: append a genuinely new record (Claude resuming work after
        # compaction). This SHOULD be forwarded.
        new_record = (
            json.dumps(
                {
                    "type": "assistant",
                    "uuid": "new-after-compaction",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": "fresh output after compaction"}],
                    },
                }
            )
            + "\n"
        )
        with transcript_path.open("a", encoding="utf-8") as f:
            f.write(new_record)

        # The new record should be the only item forwarded (the turn also
        # emits a leading running status edge, which this helper skips).
        request = await _get_recorded_item_request(server)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    # Only the post-compaction record was forwarded.
    assert request["body"]["data"]["item_data"] == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "fresh output after compaction"}],
    }


@pytest.mark.asyncio
async def test_forwarder_migrates_hook_cursor_state_to_byte_offset(tmp_path: Path) -> None:
    """
    Old hook forwarder state gains a byte cursor after one status post.

    Hook state migration must be per-record: a skipped ``SessionStart``
    and a posted ``StopFailure`` should advance the durable byte offset
    so the next poll does not rescan or repost either record.
    (``StopFailure`` is the posted-status anchor because ``Stop`` no
    longer maps to a status — idle now comes from PTY pane activity.)
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
    record_hook_event(
        bridge_dir,
        {"hook_event_name": "StopFailure", "session_id": "claude-session"},
    )
    (bridge_dir / "hook_forwarder.json").write_text(
        json.dumps({"event_cursor": 1}),
        encoding="utf-8",
    )

    server, thread, base_url = _start_recording_server()
    task = asyncio.create_task(
        forwarder.forward_claude_transcript_to_session(
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
        request = await _get_recorded_request(server)
        state = await _wait_for_json_state(
            bridge_dir / "hook_forwarder.json",
            lambda payload: payload.get("event_cursor") == 2 and "byte_offset" in payload,
        )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    context = request["body"]["data"].pop("failure_context")
    assert context["native_hook_cursor"] == 2
    assert request["body"] == {
        "type": "external_session_status",
        "data": {"status": "failed"},
    }
    assert state["event_cursor"] == 2
    assert state["byte_offset"] == (bridge_dir / "hooks.jsonl").stat().st_size


@pytest.mark.asyncio
async def test_forwarder_state_writes_run_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Cursor state persistence uses a worker thread for fsync writes.

    This catches regressions where the async forwarder calls the
    sync atomic writer directly and blocks the event loop for every
    transcript item.
    """
    main_thread_id = threading.get_ident()
    writer_thread_ids: list[int] = []
    original_write = forwarder._write_json_atomic

    def _recording_write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        """
        Record the thread used for the atomic JSON write.

        :param path: Destination JSON path.
        :param payload: JSON payload to write.
        :returns: None.
        """
        writer_thread_ids.append(threading.get_ident())
        original_write(path, payload)

    monkeypatch.setattr(forwarder, "_write_json_atomic", _recording_write_json_atomic)
    await forwarder._write_forward_state_async(
        tmp_path / "bridge",
        forwarder.TranscriptForwardState(
            transcript_path=tmp_path / "session.jsonl",
            line_cursor=0,
            byte_offset=0,
            cursor_fingerprint="fingerprint",
        ),
    )

    assert writer_thread_ids
    assert all(thread_id != main_thread_id for thread_id in writer_thread_ids)


def test_validated_transcript_state_resets_legacy_byte_cursor_without_fingerprint(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Byte-offset state without a fingerprint is treated as stale.

    A replaced transcript cannot be validated from the byte cursor
    alone. The forwarder skips to the end of the transcript to avoid
    re-posting content that was already forwarded.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "replacement",
                "message": {"role": "assistant", "content": "new file"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="omnigent.harnesses.claude_native.forwarder")

    validated = forwarder._validated_transcript_state(
        forwarder.TranscriptForwardState(
            transcript_path=transcript_path,
            line_cursor=25,
            byte_offset=0,
            current_response_id="resp_old",
            seen_source_ids=("old-source",),
            cursor_fingerprint=None,
        ),
        session_id="conv_abc",
    )

    # Cursor skips to end of transcript but preserves seen_source_ids to
    # prevent re-posting items that were already forwarded before the reset.
    expected_end = transcript_path.stat().st_size
    assert validated == forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=expected_end,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, expected_end),
        seen_source_ids=("old-source",),
    )
    assert "cursor missing fingerprint" in caplog.text
    assert "conv_abc" in caplog.text
    assert str(tmp_path / "bridge") not in caplog.text
    assert str(transcript_path) not in caplog.text


def test_validated_transcript_state_adopts_fingerprint_at_offset_zero_without_reset(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    Fresh state at byte_offset=0 with no fingerprint adopts the computed
    fingerprint without resetting seen_source_ids.

    This is the typical case when the forwarder initializes before the
    transcript file exists (fingerprint is None because the file is
    missing), and the file appears later. Since line_cursor is 0 (nothing
    has been read yet), there is no stale position — just adopt the
    fingerprint and keep going.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "first-entry",
                "message": {"role": "assistant", "content": "hello"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    caplog.set_level(logging.WARNING, logger="omnigent.harnesses.claude_native.forwarder")

    pre_existing_seen = ("already-sent-id-1", "already-sent-id-2")
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        current_response_id="resp_in_flight",
        seen_source_ids=pre_existing_seen,
        cursor_fingerprint=None,
    )

    validated = forwarder._validated_transcript_state(
        state,
        session_id="conv_fresh",
    )

    expected_fingerprint = forwarder._jsonl_cursor_fingerprint(transcript_path, 0)
    # Fingerprint adopted from the now-existing file, not reset to a blank
    # state. If the fingerprint is None here, the file doesn't exist (test
    # setup bug).
    assert validated.cursor_fingerprint == expected_fingerprint
    assert validated.cursor_fingerprint is not None

    # seen_source_ids preserved — this is the critical fix. Without it, the
    # forwarder would re-read the entire transcript and re-post every item.
    assert validated.seen_source_ids == pre_existing_seen, (
        f"Expected seen_source_ids to be preserved across fingerprint adoption, "
        f"but got {validated.seen_source_ids!r}. If empty, the dedup set was "
        f"cleared and the forwarder will re-post already-delivered items."
    )

    # Other state fields preserved (not zeroed out).
    assert validated.line_cursor == 0
    assert validated.byte_offset == 0
    assert validated.current_response_id == "resp_in_flight"

    # No warning logged — this is a clean adoption, not a stale-cursor reset.
    assert "cursor missing fingerprint" not in caplog.text
    assert "cursor invalid" not in caplog.text
    assert "cursor fingerprint changed" not in caplog.text


def test_validated_transcript_state_preserves_seen_source_ids_on_stale_reset(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    When a cursor reset IS needed (fingerprint changed because the file was
    replaced), seen_source_ids is still preserved to prevent duplicate posts.

    The cursor skips to the end of the replacement file so its existing
    content is not re-forwarded, but the dedup set keeps IDs of items
    already forwarded to the server as a safety net.
    """
    transcript_path = tmp_path / "session.jsonl"
    original_content = (
        json.dumps(
            {
                "type": "assistant",
                "uuid": "original",
                "message": {"role": "assistant", "content": "original"},
            }
        )
        + "\n"
    )
    transcript_path.write_text(original_content, encoding="utf-8")
    original_fingerprint = forwarder._jsonl_cursor_fingerprint(
        transcript_path, len(original_content.encode())
    )

    # Replace the file content — fingerprint at the old offset will differ.
    replacement_content = (
        json.dumps(
            {
                "type": "assistant",
                "uuid": "replacement",
                "message": {"role": "assistant", "content": "replaced"},
            }
        )
        + "\n"
    )
    transcript_path.write_text(replacement_content, encoding="utf-8")

    caplog.set_level(logging.WARNING, logger="omnigent.harnesses.claude_native.forwarder")

    pre_existing_seen = ("item-a", "item-b", "item-c")
    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=5,
        byte_offset=len(original_content.encode()),
        current_response_id="resp_old",
        seen_source_ids=pre_existing_seen,
        cursor_fingerprint=original_fingerprint,
    )

    validated = forwarder._validated_transcript_state(
        state,
        session_id="conv_replaced",
    )

    # Cursor skips to end of replacement file (avoids re-posting its content).
    assert validated.line_cursor == 0
    expected_end = len(replacement_content.encode())
    assert validated.byte_offset == expected_end

    # seen_source_ids preserved despite cursor reset — the critical fix.
    # Without this, every item from the replacement file would be posted
    # as new, even if some source IDs overlap with already-forwarded items.
    assert validated.seen_source_ids == pre_existing_seen, (
        f"Expected seen_source_ids to survive cursor reset, but got "
        f"{validated.seen_source_ids!r}. If empty, the dedup safety net "
        f"was destroyed and duplicates will be posted."
    )

    # Warning logged because the fingerprint genuinely changed.
    assert "cursor fingerprint changed" in caplog.text
    assert "session=conv_replaced" in caplog.text
    assert str(tmp_path / "bridge") not in caplog.text
    assert str(transcript_path) not in caplog.text
