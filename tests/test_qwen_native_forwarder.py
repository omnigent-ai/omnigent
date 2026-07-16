"""Unit tests for the qwen-native bridge + forwarder (the file-based core).

These cover the logic that diverges from goose-native: appending JSONL commands
to qwen's ``--input-file`` and parsing its ``--json-file`` stream-json events.
The event shapes are pinned to ``qwen`` v0.18.1 (see ``docs/QWEN_NATIVE_DESIGN.md``).
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from omnigent import qwen_native_bridge as qnb
from omnigent import qwen_native_forwarder as fwd
from omnigent.qwen_native_bridge import (
    BRIDGE_DIR_ENV_VAR,
    bridge_dir_for_session_id,
    build_qwen_native_spawn_env,
    events_file_path,
    input_file_path,
    prepare_bridge_files,
    qwen_session_id_for_conversation,
    qwen_session_recording_exists,
    read_tmux_info,
    submit_confirmation,
    submit_user_message,
    wait_for_ready,
    write_tmux_target,
)
from omnigent.qwen_native_forwarder import (
    _DEDUP_WINDOW,
    _compaction_status_from_record,
    _event_to_items,
    _ForwardState,
    _new_seen,
    _read_new_compaction_statuses,
    _read_new_records,
    _read_state,
    _records_to_actions,
    _write_state,
    clear_qwen_bridge_state,
)

_AGENT = "qwen-native-ui"


def _user_ev(uuid: str, text: str) -> dict:
    return {
        "type": "user",
        "uuid": uuid,
        "message": {"role": "user", "content": [{"type": "text", "text": text}]},
    }


def _asst_ev(uuid: str, content: list[dict]) -> dict:
    return {
        "type": "assistant",
        "uuid": uuid,
        "message": {"role": "assistant", "content": content},
    }


def _tool_use_block(block_id: str, name: str, tool_input: dict) -> dict:
    return {"type": "tool_use", "id": block_id, "name": name, "input": tool_input}


def _tool_result_ev(uuid: str, tool_use_id: str, output: str) -> dict:
    """qwen returns tool results as a ``user`` event in the Anthropic envelope."""
    return {
        "type": "user",
        "uuid": uuid,
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": [{"type": "text", "text": output}],
                }
            ],
        },
    }


def _result_ev(subtype: str = "success", result: str = "done") -> dict:
    return {"type": "result", "subtype": subtype, "result": result}


def _ev_bytes(obj: dict) -> bytes:
    return (json.dumps(obj) + "\n").encode("utf-8")


def test_user_event_maps_to_input_text() -> None:
    items = _event_to_items(_user_ev("u1", "hi"), _AGENT, "qwen:turn:u1")
    assert len(items) == 1
    assert items[0].response_id == "qwen:turn:u1"
    assert items[0].item_data == {
        "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],
    }


def test_assistant_text_maps_to_output_text_and_skips_thinking() -> None:
    items = _event_to_items(
        _asst_ev(
            "a1",
            [
                {"type": "thinking", "thinking": "secret reasoning"},
                {"type": "text", "text": "Hi there!"},
            ],
        ),
        _AGENT,
        "qwen:turn:u1",
    )
    assert len(items) == 1
    assert items[0].item_data["role"] == "assistant"
    assert items[0].item_data["agent"] == _AGENT
    assert items[0].item_data["content"] == [{"type": "output_text", "text": "Hi there!"}]


def test_thinking_only_assistant_skipped() -> None:
    assert (
        _event_to_items(_asst_ev("a2", [{"type": "thinking", "thinking": "x"}]), _AGENT, None)
        == []
    )


def test_non_message_events_ignored() -> None:
    for etype in ("system", "stream_event", "result"):
        assert _event_to_items({"type": etype, "uuid": "x"}, _AGENT, None) == []
    # control_request is recognized but produces no mirror item.
    control = {
        "type": "control_request",
        "request": {"subtype": "can_use_tool", "tool_name": "shell"},
        "request_id": "r1",
    }
    assert _event_to_items(control, _AGENT, None) == []


def test_attachment_marker_stripped() -> None:
    items = _event_to_items(_user_ev("u2", "[Attached: /tmp/x.png]\n\nlook"), _AGENT, None)
    assert items[0].item_data["content"][0]["text"] == "look"


# --- tool-call mirroring (live cards) ---------------------------------------


def test_assistant_prose_then_tool_uses_emits_message_then_function_calls() -> None:
    items = _event_to_items(
        _asst_ev(
            "a1",
            [
                {"type": "text", "text": "I'll look."},
                _tool_use_block("t1", "read_file", {"path": "/x"}),
                _tool_use_block("t2", "shell", {"cmd": "ls"}),
            ],
        ),
        _AGENT,
        "qwen:turn:u1",
    )
    # Prose FIRST: the web only spins the TRAILING tool phase.
    assert [i.item_type for i in items] == ["message", "function_call", "function_call"]
    assert all(i.response_id == "qwen:turn:u1" for i in items)
    assert items[1].item_data == {
        "agent": _AGENT,
        "name": "read_file",
        "arguments": json.dumps({"path": "/x"}),
        "call_id": "t1",
    }
    # ``arguments`` is a JSON *string*, matching the function_call item contract.
    assert isinstance(items[1].item_data["arguments"], str)
    assert items[2].item_data["call_id"] == "t2"


def test_tool_use_only_assistant_emits_function_call() -> None:
    # Regression: a tool-only assistant event used to be dropped entirely, so the
    # web never saw the call and could not render a card for it.
    items = _event_to_items(
        _asst_ev("a1", [_tool_use_block("t1", "shell", {"cmd": "ls"})]), _AGENT, "qwen:turn:u1"
    )
    assert [i.item_type for i in items] == ["function_call"]


def test_tool_result_user_event_emits_function_call_output() -> None:
    items = _event_to_items(_tool_result_ev("u9", "t1", "file contents"), _AGENT, "qwen:turn:u1")
    assert [i.item_type for i in items] == ["function_call_output"]
    assert items[0].item_data == {"call_id": "t1", "output": "file contents"}
    assert items[0].response_id == "qwen:turn:u1"


def test_malformed_tool_blocks_skipped_not_fatal() -> None:
    # A tool_use missing id/name, and a tool_result missing tool_use_id, degrade to
    # a dropped card rather than stalling the tail.
    assert _event_to_items(_asst_ev("a1", [{"type": "tool_use", "input": {}}]), _AGENT, "t") == []
    bad_result = {
        "type": "user",
        "uuid": "u1",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "x"}]},
    }
    assert _event_to_items(bad_result, _AGENT, "t") == []


# --- turn ids + status edges -------------------------------------------------


def test_prose_user_event_opens_turn_and_stamps_all_items() -> None:
    records = [
        _user_ev("u1", "list files"),
        _asst_ev("a1", [_tool_use_block("t1", "shell", {"cmd": "ls"})]),
        _tool_result_ev("u2", "t1", "a.txt"),
        _asst_ev("a2", [{"type": "text", "text": "Found a.txt."}]),
    ]
    actions, turn_id, live = _records_to_actions(records, _AGENT, set(), None, False)
    ids = {a.turn_id for a in actions}
    assert ids == {"qwen:turn:u1"}
    assert turn_id == "qwen:turn:u1"
    # running fires once, before the turn's first assistant item.
    kinds = [(a.kind, a.status or (a.item.item_type if a.item else None)) for a in actions]
    assert kinds == [
        ("item", "message"),  # the user prompt bubble
        ("status", "running"),
        ("item", "function_call"),
        ("item", "function_call_output"),
        ("item", "message"),  # the assistant's answer
        ("status", "idle"),  # prose-only assistant step closes the card
    ]
    assert live is False


def test_tool_result_user_event_does_not_open_a_new_turn() -> None:
    # Regression: tool results arrive as ``user`` events in the Anthropic
    # envelope. Treating every user event as a turn delimiter would split the turn
    # at each tool call and kill the spinner on exactly the long calls the live
    # card exists for.
    records = [
        _user_ev("u1", "go"),
        _asst_ev("a1", [_tool_use_block("t1", "shell", {"cmd": "ls"})]),
        _tool_result_ev("u2", "t1", "out"),
        _asst_ev("a2", [_tool_use_block("t2", "shell", {"cmd": "pwd"})]),
    ]
    actions, turn_id, live = _records_to_actions(records, _AGENT, set(), None, False)
    assert turn_id == "qwen:turn:u1"  # NOT qwen:turn:u2
    assert {a.turn_id for a in actions} == {"qwen:turn:u1"}
    # Exactly one running edge for the whole turn — no flicker at the tool result.
    assert [a.status for a in actions if a.kind == "status"] == ["running"]
    assert live is True


def test_result_success_closes_turn_with_idle() -> None:
    records = [
        _user_ev("u1", "go"),
        _asst_ev("a1", [_tool_use_block("t1", "shell", {"cmd": "ls"})]),
        _result_ev("success"),
    ]
    actions, turn_id, live = _records_to_actions(records, _AGENT, set(), None, False)
    statuses = [(a.status, a.turn_id) for a in actions if a.kind == "status"]
    assert statuses == [("running", "qwen:turn:u1"), ("idle", "qwen:turn:u1")]
    assert turn_id is None and live is False


def test_result_failure_closes_turn_with_failed_and_detail() -> None:
    records = [
        _user_ev("u1", "go"),
        _asst_ev("a1", [_tool_use_block("t1", "shell", {"cmd": "ls"})]),
        _result_ev("error_max_turns", result="hit the turn cap"),
    ]
    actions, _turn_id, live = _records_to_actions(records, _AGENT, set(), None, False)
    closing = [a for a in actions if a.kind == "status"][-1]
    assert closing.status == "failed"
    assert closing.output == "hit the turn cap"
    assert live is False


def test_result_failure_after_prose_close_still_posts_failed() -> None:
    # A prose-only final step settles the card to idle; a late failure ``result``
    # must override that premature idle with ``failed`` + detail rather than drop.
    records = [
        _user_ev("u1", "go"),
        _asst_ev("a1", [{"type": "text", "text": "all done"}]),
        _result_ev("error_during_execution", result="boom"),
    ]
    actions, turn_id, live = _records_to_actions(records, _AGENT, set(), None, False)
    statuses = [(a.status, a.turn_id, a.output) for a in actions if a.kind == "status"]
    assert statuses == [
        ("running", "qwen:turn:u1", None),
        ("idle", "qwen:turn:u1", None),
        ("failed", "qwen:turn:u1", "boom"),
    ]
    assert turn_id is None and live is False


def test_result_success_after_prose_close_does_not_repost_status() -> None:
    # A success ``result`` after the prose ``idle`` is already settled — no
    # duplicate idle, and certainly no spurious failed.
    records = [
        _user_ev("u1", "go"),
        _asst_ev("a1", [{"type": "text", "text": "all done"}]),
        _result_ev("success"),
    ]
    actions, turn_id, live = _records_to_actions(records, _AGENT, set(), None, False)
    statuses = [(a.status, a.turn_id) for a in actions if a.kind == "status"]
    assert statuses == [("running", "qwen:turn:u1"), ("idle", "qwen:turn:u1")]
    assert turn_id is None and live is False


def test_two_turns_get_disjoint_ids_and_paired_edges() -> None:
    records = [
        _user_ev("u1", "one"),
        _asst_ev("a1", [{"type": "text", "text": "first"}]),
        _result_ev("success"),
        _user_ev("u2", "two"),
        _asst_ev("a2", [{"type": "text", "text": "second"}]),
        _result_ev("success"),
    ]
    actions, _turn_id, _live = _records_to_actions(records, _AGENT, set(), None, False)
    statuses = [(a.status, a.turn_id) for a in actions if a.kind == "status"]
    # The prose-only assistant closes each turn; ``result`` then finds it closed.
    assert statuses == [
        ("running", "qwen:turn:u1"),
        ("idle", "qwen:turn:u1"),
        ("running", "qwen:turn:u2"),
        ("idle", "qwen:turn:u2"),
    ]


def test_new_prose_user_event_closes_a_still_open_turn() -> None:
    # A turn interrupted by the next prompt (no ``result``) still gets its idle.
    records = [_user_ev("u2", "actually, stop")]
    actions, turn_id, live = _records_to_actions(records, _AGENT, set(), "qwen:turn:u1", True)
    assert (actions[0].kind, actions[0].status, actions[0].turn_id) == (
        "status",
        "idle",
        "qwen:turn:u1",
    )
    assert turn_id == "qwen:turn:u2" and live is False


def test_assistant_without_open_turn_mints_one() -> None:
    # Missed-start recovery: a forwarder attaching mid-turn still gets an id, so
    # the rest of the turn's cards render live.
    actions, turn_id, live = _records_to_actions(
        [_asst_ev("a1", [_tool_use_block("t1", "shell", {"cmd": "ls"})])],
        _AGENT,
        set(),
        None,
        False,
    )
    assert turn_id == "qwen:turn:a1"
    assert [a.status for a in actions if a.kind == "status"] == ["running"]
    assert live is True


def test_running_not_re_posted_when_turn_already_live() -> None:
    actions, _turn_id, live = _records_to_actions(
        [_asst_ev("a1", [_tool_use_block("t1", "shell", {"cmd": "ls"})])],
        _AGENT,
        set(),
        "qwen:turn:u1",
        True,
    )
    assert [a.kind for a in actions] == ["item"]
    assert live is True


def test_seen_uuids_suppress_reposted_items() -> None:
    # A truncation rewind re-reads the file; already-posted uuids must not re-post.
    actions, _turn_id, _live = _records_to_actions(
        [_user_ev("u1", "go"), _asst_ev("a1", [{"type": "text", "text": "hi"}])],
        _AGENT,
        {"u1", "a1"},
        None,
        False,
    )
    assert [a for a in actions if a.kind == "item"] == []


# --- raw record tail ---------------------------------------------------------


def test_read_new_records_incremental_and_partial_line(tmp_path: Path) -> None:
    f = tmp_path / "out.ndjson"
    f.write_bytes(_ev_bytes(_user_ev("u1", "q")))
    records, off = _read_new_records(f, 0)
    assert [r["uuid"] for r in records] == ["u1"]
    assert off == f.stat().st_size

    # Append a complete assistant line plus a trailing *partial* line.
    with open(f, "ab") as fh:
        fh.write(_ev_bytes(_asst_ev("a1", [{"type": "text", "text": "a"}])))
        fh.flush()
        complete_size = f.stat().st_size
        fh.write(b'{"type":"assistant","uuid":"a2"')  # no newline yet
        fh.flush()
    records, off2 = _read_new_records(f, off)
    assert [r["uuid"] for r in records] == ["a1"]
    # Offset stops at the last newline — the partial line is not consumed.
    assert off2 == complete_size


def test_read_new_records_detects_truncation(tmp_path: Path) -> None:
    f = tmp_path / "out.ndjson"
    # A long first line so the stale offset exceeds the post-truncation size.
    f.write_bytes(_ev_bytes(_user_ev("u1", "first message, intentionally long " * 4)))
    _, off = _read_new_records(f, 0)
    assert off > 0
    # A relaunched terminal truncates + writes a shorter line; size < offset
    # must rewind so the fresh content is not skipped.
    f.write_bytes(_ev_bytes(_user_ev("u2", "fresh")))
    assert f.stat().st_size < off
    records, _ = _read_new_records(f, off)
    assert [r["uuid"] for r in records] == ["u2"]


def test_malformed_line_tolerated(tmp_path: Path) -> None:
    f = tmp_path / "out.ndjson"
    f.write_bytes(b"not json\n" + _ev_bytes(_user_ev("u1", "ok")))
    records, _ = _read_new_records(f, 0)
    assert [r["uuid"] for r in records] == ["u1"]


# --- Compaction mirror (chat-recording tail) -------------------------------


def _compression_record(status: int) -> dict:
    """A qwen chat-recording ``chat_compression`` system event."""
    return {
        "type": "system",
        "subtype": "chat_compression",
        "systemPayload": {
            "info": {
                "originalTokenCount": 19398,
                "newTokenCount": 17000,
                "compressionStatus": status,
            }
        },
    }


def test_compaction_status_from_record() -> None:
    assert _compaction_status_from_record(_compression_record(1)) == "completed"
    # COMPRESSION_FAILED_* statuses → failed.
    assert _compaction_status_from_record(_compression_record(2)) == "failed"
    assert _compaction_status_from_record(_compression_record(3)) == "failed"
    # Other recording lines are ignored.
    assert _compaction_status_from_record({"type": "system", "subtype": "ui_telemetry"}) is None
    assert _compaction_status_from_record(_user_ev("u1", "hi")) is None


def test_read_new_compaction_statuses_incremental_and_ignores_other_lines(tmp_path: Path) -> None:
    rec = tmp_path / "chat.jsonl"
    # A transcript line + a successful compression record.
    rec.write_bytes(_ev_bytes(_user_ev("u1", "hi")) + _ev_bytes(_compression_record(1)))
    statuses, off = _read_new_compaction_statuses(rec, 0)
    assert statuses == ["completed"]
    assert off == rec.stat().st_size
    # No new lines → nothing.
    assert _read_new_compaction_statuses(rec, off) == ([], off)


def test_read_new_compaction_statuses_detects_truncation(tmp_path: Path) -> None:
    rec = tmp_path / "chat.jsonl"
    rec.write_bytes(_ev_bytes(_user_ev("u1", "padding line, intentionally long " * 4)))
    off = rec.stat().st_size
    # A recreated (shorter) recording must rewind so a fresh compaction is seen.
    rec.write_bytes(_ev_bytes(_compression_record(1)))
    assert rec.stat().st_size < off
    statuses, _ = _read_new_compaction_statuses(rec, off)
    assert statuses == ["completed"]


def test_read_new_compaction_statuses_missing_file(tmp_path: Path) -> None:
    # Recording not created yet → no statuses, offset unchanged (retry next poll).
    assert _read_new_compaction_statuses(tmp_path / "absent.jsonl", 0) == ([], 0)


def test_wait_for_ready_times_out_without_boot_signal(tmp_path: Path) -> None:
    bridge = tmp_path / "bridge"
    prepare_bridge_files(bridge)
    # No events written → never ready; returns False fast (tiny timeout).
    assert wait_for_ready(bridge, timeout_s=0.05, poll_interval_s=0.01) is False


def test_wait_for_ready_detects_system_event(tmp_path: Path) -> None:
    bridge = tmp_path / "bridge"
    prepare_bridge_files(bridge)
    # qwen emits a compact system/session_start as its first event.
    events_file_path(bridge).write_bytes(
        _ev_bytes({"type": "system", "subtype": "session_start", "uuid": "s1"})
    )
    assert wait_for_ready(bridge, timeout_s=1.0, poll_interval_s=0.01) is True


def test_wait_for_ready_ignores_system_substring_in_non_system_event(tmp_path: Path) -> None:
    bridge = tmp_path / "bridge"
    prepare_bridge_files(bridge)
    # An assistant event whose text payload contains the bytes '"type":"system"'
    # must NOT be read as the boot signal — readiness parses per-line and checks
    # event["type"], so a substring inside another event can't latch ready early.
    events_file_path(bridge).write_bytes(
        _ev_bytes(_asst_ev("a1", [{"type": "text", "text": 'note "type":"system" inside'}]))
    )
    assert wait_for_ready(bridge, timeout_s=0.05, poll_interval_s=0.01) is False


def test_qwen_session_id_is_deterministic_and_uuid() -> None:
    a = qwen_session_id_for_conversation("conv_abc123")
    b = qwen_session_id_for_conversation("conv_abc123")
    c = qwen_session_id_for_conversation("conv_other")
    assert a == b  # stable across calls → resume can recompute it
    assert a != c  # distinct per conversation
    # Valid UUID (qwen requires one for --session-id / --resume).
    import uuid as _uuid

    assert str(_uuid.UUID(a)) == a


def test_qwen_session_recording_exists_is_workspace_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.qwen_native_bridge import _qwen_project_slug

    monkeypatch.setenv("HOME", str(tmp_path))
    ws_a = tmp_path / "repo_a"
    ws_a.mkdir()
    ws_b = tmp_path / "repo_b"
    ws_b.mkdir()
    sid = qwen_session_id_for_conversation("conv_resume_me")
    # No recording yet → fresh launch (--session-id).
    assert qwen_session_recording_exists(sid, ws_a) is False
    # qwen records under the LAUNCH workspace's project slug.
    chats = tmp_path / ".qwen" / "projects" / _qwen_project_slug(ws_a) / "chats"
    chats.mkdir(parents=True)
    (chats / f"{sid}.jsonl").write_text("{}\n", encoding="utf-8")
    assert qwen_session_recording_exists(sid, ws_a) is True
    # Resuming the SAME conversation from a DIFFERENT workspace must NOT see it:
    # qwen --resume is per-project, so a cross-workspace True would pick --resume
    # and land on qwen's blocking "No saved session found" screen.
    assert qwen_session_recording_exists(sid, ws_b) is False
    # A different conversation's id under the same workspace is unaffected.
    assert qwen_session_recording_exists(qwen_session_id_for_conversation("conv_x"), ws_a) is False


def test_qwen_session_recording_path_is_workspace_scoped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.qwen_native_bridge import _qwen_project_slug, qwen_session_recording_path

    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / "repo"
    ws.mkdir()
    sid = qwen_session_id_for_conversation("conv_rec")
    expected = tmp_path / ".qwen" / "projects" / _qwen_project_slug(ws) / "chats" / f"{sid}.jsonl"
    assert qwen_session_recording_path(sid, ws) == expected


def test_bridge_submit_and_confirmation_append_jsonl(tmp_path: Path) -> None:
    bridge = tmp_path / "bridge"
    prepare_bridge_files(bridge)
    in_file = input_file_path(bridge)
    assert in_file.exists() and in_file.read_text() == ""

    submit_user_message(bridge, content="hello world")
    submit_confirmation(bridge, request_id="r1", allowed=True)

    lines = [json.loads(line) for line in in_file.read_text().splitlines() if line.strip()]
    assert lines[0] == {"type": "submit", "text": "hello world"}
    assert lines[1] == {"type": "confirmation_response", "request_id": "r1", "allowed": True}


def test_forward_state_roundtrip_and_clear(tmp_path: Path) -> None:
    state = _ForwardState(offset=123, seen_uuids=["a", "b"], active_turn_id="qwen:turn:u1")
    assert _write_state(tmp_path, state) is True
    loaded = _read_state(tmp_path)
    assert loaded.offset == 123
    assert loaded.seen_uuids == ["a", "b"]
    # The open turn id survives a restart so a resumed tail rejoins the same turn
    # instead of splitting its streaming group across two ids.
    assert loaded.active_turn_id == "qwen:turn:u1"
    # Clearing resets the cursor so a re-created terminal starts clean.
    clear_qwen_bridge_state(tmp_path)
    cleared = _read_state(tmp_path)
    assert cleared.offset == 0
    assert cleared.seen_uuids == []
    assert cleared.active_turn_id is None


def test_forward_state_caps_seen_uuids(tmp_path: Path) -> None:
    # The dedup window is bounded so the state file can't grow unbounded.
    state = _ForwardState(offset=1, seen_uuids=[str(i) for i in range(1000)])
    assert _write_state(tmp_path, state) is True
    loaded = _read_state(tmp_path)
    assert len(loaded.seen_uuids or []) == 512
    assert (loaded.seen_uuids or [])[-1] == "999"  # most-recent retained


def test_seen_dedup_window_keeps_most_recent_in_insertion_order(tmp_path: Path) -> None:
    """The persisted dedup window retains the *most recent* uuids, in order.

    Regression: ``seen`` used to be a ``set``, so the forwarder's
    ``list(seen)`` at persist time was hash-ordered and ``_write_state``'s
    ``[-_DEDUP_WINDOW:]`` cap kept an arbitrary subset — not the most recent.
    On a qwen relaunch past ``_DEDUP_WINDOW`` events (offset rewinds to 0 and
    the file is re-read from the top), recent uuids evicted from the window
    were re-posted as duplicate bubbles. Building ``seen`` through
    :func:`_new_seen` (an insertion-ordered ``dict``) keeps the real tail.

    This exercises the forwarder's own reload/persist path — ``_new_seen`` (the
    line-301 ``seen = _new_seen(persisted.seen_uuids)`` idiom) then
    ``list(seen)`` — so a revert to a ``set`` fails the ordering assertion here
    (unlike ``test_forward_state_caps_seen_uuids``, which feeds an
    already-ordered list straight into ``_write_state`` and so never sees the
    ordering bug).
    """
    uuids = [f"u{i:05d}" for i in range(_DEDUP_WINDOW * 2)]
    seen = _new_seen(uuids)

    assert _write_state(tmp_path, _ForwardState(offset=7, seen_uuids=list(seen))) is True

    kept = _read_state(tmp_path).seen_uuids or []
    # The cap must keep the most-recent _DEDUP_WINDOW uuids, in order — not an
    # arbitrary hash-ordered subset (which a set-backed ``seen`` produced).
    assert kept == uuids[-_DEDUP_WINDOW:]


def test_tmux_target_round_trip(tmp_path: Path) -> None:
    write_tmux_target(tmp_path, socket_path=Path("/tmp/qwen.sock"), tmux_target="sess:0.0")
    info = read_tmux_info(tmp_path)
    assert info == {"socket_path": "/tmp/qwen.sock", "tmux_target": "sess:0.0"}


def test_read_tmux_info_missing(tmp_path: Path) -> None:
    assert read_tmux_info(tmp_path) is None


def test_spawn_env_carries_bridge_dir() -> None:
    env = build_qwen_native_spawn_env("conv_spawn_env")
    assert env[BRIDGE_DIR_ENV_VAR] == str(bridge_dir_for_session_id("conv_spawn_env"))


def test_harness_registered_aliased_and_native() -> None:
    from omnigent.harness_aliases import canonicalize_harness, is_native_harness
    from omnigent.native_coding_agents import native_coding_agent_for_harness
    from omnigent.runtime.harnesses import _HARNESS_MODULES
    from omnigent.spec._omnigent_compat import OMNIGENT_HARNESSES

    # Registry entry resolves to the harness module.
    assert _HARNESS_MODULES["qwen-native"] == "omnigent.inner.qwen_native_harness"
    # Allowlisted + recognized as a native-terminal harness (both spellings).
    assert "qwen-native" in OMNIGENT_HARNESSES
    assert is_native_harness("qwen-native") is True
    assert is_native_harness("native-qwen") is True
    assert canonicalize_harness("native-qwen") == "qwen-native"
    # Native coding-agent metadata is wired for the picker / labels.
    agent = native_coding_agent_for_harness("qwen-native")
    assert agent is not None
    assert agent.terminal_name == "qwen"
    assert agent.display_name == "Qwen Code"
    assert agent.agent_name == "qwen-native-ui"


def test_harness_create_app_builds() -> None:
    from omnigent.inner.qwen_native_harness import create_app

    app = create_app()
    assert app is not None


# --- bridge tmux helpers (interrupt / hard-stop) -----------------------------


class _FakeProc:
    """Minimal ``subprocess.run`` result stand-in."""

    def __init__(self, returncode: int = 0, stderr: str = "", stdout: str = "") -> None:
        self.returncode = returncode
        self.stderr = stderr
        self.stdout = stdout


def test_inject_interrupt_sends_escape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_tmux_target(tmp_path, socket_path=Path("/tmp/q.sock"), tmux_target="sess:0.0")
    captured: list[list[str]] = []
    monkeypatch.setattr(
        qnb.subprocess, "run", lambda cmd, **_k: captured.append(cmd) or _FakeProc(0)
    )
    qnb.inject_interrupt(tmp_path, timeout_s=1.0)
    # No ``-l`` flag: tmux interprets ``Escape`` as a key name, not literal text.
    assert captured[0] == ["tmux", "-S", "/tmp/q.sock", "send-keys", "-t", "sess:0.0", "Escape"]


def test_kill_session_kills_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write_tmux_target(tmp_path, socket_path=Path("/tmp/q.sock"), tmux_target="sess:0.0")
    captured: list[list[str]] = []
    monkeypatch.setattr(
        qnb.subprocess, "run", lambda cmd, **_k: captured.append(cmd) or _FakeProc(0)
    )
    qnb.kill_session(tmp_path, timeout_s=1.0)
    assert captured[0] == ["tmux", "-S", "/tmp/q.sock", "kill-session", "-t", "sess:0.0"]


def test_run_tmux_raises_on_nonzero(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(qnb.subprocess, "run", lambda *_a, **_k: _FakeProc(1, stderr="boom"))
    with pytest.raises(RuntimeError, match="boom"):
        qnb._run_tmux("/tmp/q.sock", "send-keys")


def test_inject_interrupt_raises_when_target_unadvertised(tmp_path: Path) -> None:
    # No tmux.json written → the wait times out fast and raises.
    with pytest.raises(RuntimeError):
        qnb.inject_interrupt(tmp_path, timeout_s=0.05)


# --- forwarder: post + poll loop + supervisor --------------------------------


class _RecordingClient:
    """Async httpx-client stub that records POSTs and returns HTTP 200."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, *, json: dict) -> httpx.Response:
        self.posts.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))


async def test_post_conversation_item_shape() -> None:
    client = _RecordingClient()
    item = fwd._MirrorItem(
        uuid="u1",
        item_type="message",
        item_data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        response_id="qwen:u1",
    )
    await fwd._post_conversation_item(client, session_id="conv_1", item=item)  # type: ignore[arg-type]
    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_1/events"
    assert body["type"] == "external_conversation_item"
    assert body["data"] == {
        "item_type": "message",
        "item_data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        "response_id": "qwen:u1",
    }


def _install_post_recorders(monkeypatch: pytest.MonkeyPatch, log: list[tuple]) -> None:
    """Record item + status POSTs into one ordered log, so edge ORDER is assertable."""

    async def _fake_item(_client: object, *, session_id: str, item: object) -> None:
        log.append(("item", item.item_type, item.response_id))  # type: ignore[attr-defined]

    async def _fake_status(
        _client: object,
        *,
        session_id: str,
        status: str,
        output: str | None = None,
        background_task_count: int | None = None,
        response_id: str | None = None,
    ) -> None:
        log.append(("status", status, response_id))

    monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)
    monkeypatch.setattr(fwd, "post_external_session_status", _fake_status)


async def _drive_forward_loop(bridge: Path, log: list[tuple], *, until: int) -> None:
    """Run the poll loop until *until* posts land, then cancel it cleanly."""
    task = asyncio.create_task(
        fwd.forward_qwen_events_to_session(
            base_url="http://test",
            headers={},
            session_id="conv",
            bridge_dir=bridge,
            agent_name=_AGENT,
            poll_interval_s=0.01,
        )
    )
    for _ in range(200):
        if len(log) >= until:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    # Awaiting the cancelled task must propagate CancelledError (clean teardown).
    with pytest.raises(asyncio.CancelledError):
        _ = await task


async def test_forward_loop_posts_new_events_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    events_file_path(bridge).write_bytes(_ev_bytes(_user_ev("u1", "hello")))

    log: list[tuple] = []
    _install_post_recorders(monkeypatch, log)
    await _drive_forward_loop(bridge, log, until=1)

    assert log == [("item", "message", "qwen:turn:u1")]
    # Offset + dedup set are persisted so a restart resumes without re-posting.
    state = _read_state(bridge)
    assert state.offset > 0
    assert "u1" in (state.seen_uuids or [])
    # The turn is still open at the cursor, so its id is persisted too.
    assert state.active_turn_id == "qwen:turn:u1"


async def test_forward_loop_brackets_tool_calls_with_id_bearing_status_edges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point: a turn's tool cards ride between running/idle on ONE id.

    That pairing is what makes ap-web render the cards live (spinner + elapsed
    timer) instead of as static, already-completed cards.
    """
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    events_file_path(bridge).write_bytes(
        _ev_bytes(_user_ev("u1", "list files"))
        + _ev_bytes(_asst_ev("a1", [_tool_use_block("t1", "shell", {"cmd": "ls"})]))
        + _ev_bytes(_tool_result_ev("u2", "t1", "a.txt"))
        + _ev_bytes(_result_ev("success"))
    )

    log: list[tuple] = []
    _install_post_recorders(monkeypatch, log)
    await _drive_forward_loop(bridge, log, until=5)

    assert log == [
        ("item", "message", "qwen:turn:u1"),
        ("status", "running", "qwen:turn:u1"),
        ("item", "function_call", "qwen:turn:u1"),
        ("item", "function_call_output", "qwen:turn:u1"),
        ("status", "idle", "qwen:turn:u1"),
    ]
    # ``result`` closed the turn, so nothing is left open at the cursor.
    assert _read_state(bridge).active_turn_id is None


async def test_forward_loop_resumes_open_turn_after_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restart mid-turn keeps the turn id, so the cards stay in one group."""
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    events_file_path(bridge).write_bytes(
        _ev_bytes(_user_ev("u1", "go"))
        + _ev_bytes(_asst_ev("a1", [_tool_use_block("t1", "shell", {"cmd": "ls"})]))
    )

    log: list[tuple] = []
    _install_post_recorders(monkeypatch, log)
    await _drive_forward_loop(bridge, log, until=3)
    assert _read_state(bridge).active_turn_id == "qwen:turn:u1"

    # The turn's tail lands while the forwarder is down; a fresh loop picks it up.
    with open(events_file_path(bridge), "ab") as fh:
        fh.write(_ev_bytes(_tool_result_ev("u2", "t1", "a.txt")))
        fh.write(_ev_bytes(_result_ev("success")))

    log.clear()
    await _drive_forward_loop(bridge, log, until=2)
    # Resumed items keep the ORIGINAL turn id, and the orphaned running is closed.
    assert log == [
        ("item", "function_call_output", "qwen:turn:u1"),
        ("status", "idle", "qwen:turn:u1"),
    ]


async def test_forward_loop_stalled_turn_backstop_closes_card(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn killed without its terminator must not spin forever."""
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    # A turn that opens a tool call and then goes silent (TUI interrupt / crash).
    events_file_path(bridge).write_bytes(
        _ev_bytes(_user_ev("u1", "go"))
        + _ev_bytes(_asst_ev("a1", [_tool_use_block("t1", "shell", {"cmd": "sleep 999"})]))
    )

    log: list[tuple] = []
    _install_post_recorders(monkeypatch, log)
    # Stub the clock rather than sleeping out the real minutes-scale backstop.
    clock = {"t": 0.0}
    monkeypatch.setattr(fwd, "_monotonic", lambda: clock["t"])

    task = asyncio.create_task(
        fwd.forward_qwen_events_to_session(
            base_url="http://test",
            headers={},
            session_id="conv",
            bridge_dir=bridge,
            agent_name=_AGENT,
            poll_interval_s=0.01,
        )
    )
    for _ in range(200):
        if len(log) >= 3:
            break
        await asyncio.sleep(0.01)
    assert [e[1] for e in log] == ["message", "running", "function_call"]

    # Jump past the backstop with no new events — the spinner gets settled.
    clock["t"] = fwd._STALLED_TURN_IDLE_S + 1.0
    for _ in range(200):
        if len(log) >= 4:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await task

    assert log[-1] == ("status", "idle", "qwen:turn:u1")


async def test_forward_loop_backstop_settles_resumed_live_turn_without_new_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashed live turn resumed on restart must settle even if the file never grows."""
    bridge = tmp_path / "bridge"
    bridge.mkdir()
    # The event file holds the turn's tool call but will not grow (qwen crashed).
    events_file_path(bridge).write_bytes(
        _ev_bytes(_user_ev("u1", "go"))
        + _ev_bytes(_asst_ev("a1", [_tool_use_block("t1", "shell", {"cmd": "sleep 999"})]))
    )
    eof = events_file_path(bridge).stat().st_size
    # Persist the live turn at EOF, exactly as the crashed process left it.
    _write_state(
        bridge,
        _ForwardState(
            offset=eof,
            seen_uuids=["u1", "a1", "t1"],
            active_turn_id="qwen:turn:u1",
            turn_live=True,
        ),
    )

    log: list[tuple] = []
    _install_post_recorders(monkeypatch, log)
    clock = {"t": 0.0}
    monkeypatch.setattr(fwd, "_monotonic", lambda: clock["t"])

    task = asyncio.create_task(
        fwd.forward_qwen_events_to_session(
            base_url="http://test",
            headers={},
            session_id="conv",
            bridge_dir=bridge,
            agent_name=_AGENT,
            poll_interval_s=0.01,
        )
    )
    # No new events ever arrive; advance past the backstop window.
    await asyncio.sleep(0.05)
    clock["t"] = fwd._STALLED_TURN_IDLE_S + 1.0
    for _ in range(200):
        if log:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await task

    # The orphaned running is closed even though the file never grew post-restart.
    assert log == [("status", "idle", "qwen:turn:u1")]


async def test_compaction_mirror_seeds_at_eof_and_posts_new(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rec = tmp_path / "chat.jsonl"
    # A pre-existing compression record (resume case): must NOT be re-posted.
    rec.write_bytes(_ev_bytes(_compression_record(1)))

    posted: list[str] = []

    async def _fake_post(_client: object, *, session_id: str, status: str) -> None:
        posted.append(status)

    monkeypatch.setattr(fwd, "_post_external_compaction_status", _fake_post)

    task = asyncio.create_task(
        fwd.supervise_qwen_compaction_mirror(
            base_url="http://test",
            headers={},
            session_id="conv",
            recording_path=rec,
            poll_interval_s=0.01,
        )
    )
    # Let it seed at EOF; the pre-existing record stays unposted.
    await asyncio.sleep(0.05)
    assert posted == []
    # A new compression now lands and is mirrored.
    with open(rec, "ab") as fh:
        fh.write(_ev_bytes(_compression_record(1)))
    for _ in range(200):
        if posted:
            break
        await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        _ = await task
    assert posted == ["completed"]


async def test_supervise_restarts_then_propagates_cancel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"n": 0}
    sleeps: list[float] = []

    async def _fake_forward(**_kw: object) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("boom")  # first run crashes → supervisor restarts
        raise asyncio.CancelledError()  # second run cancelled → propagates out

    async def _fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(fwd, "forward_qwen_events_to_session", _fake_forward)
    monkeypatch.setattr(fwd, "_supervisor_sleep", _fake_sleep)
    # Never "healthy" (uptime 0) so the backoff is not reset between runs.
    monkeypatch.setattr(fwd, "_supervisor_monotonic", lambda: 0.0)

    with pytest.raises(asyncio.CancelledError):
        await fwd.supervise_qwen_forwarder(
            base_url="http://test",
            headers={},
            session_id="conv",
            bridge_dir=tmp_path,
            agent_name=_AGENT,
        )

    assert calls["n"] == 2
    assert sleeps == [1.0]  # initial backoff before the one restart
