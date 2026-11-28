"""Unit tests for the commandcode-native transcript forwarder."""

from __future__ import annotations

import json
import os
from pathlib import Path

from omnigent.harnesses.commandcode_native import forwarder as fwd


def _write_session(root: Path, workspace: str, session_id: str, entries: list[dict]) -> Path:
    slug_dir = root / "projects" / fwd._project_slug(workspace)
    slug_dir.mkdir(parents=True, exist_ok=True)
    path = slug_dir / f"{session_id}.jsonl"
    lines = [
        json.dumps(
            {
                "type": "session",
                "version": 3,
                "id": session_id,
                "timestamp": "2026-11-05T11:56:32.178Z",
                "cwd": workspace,
            }
        )
    ]
    lines.extend(json.dumps(entry) for entry in entries)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _msg(role: str, source: str, content: list[dict], mid: str = "m1") -> dict:
    return {
        "type": "message",
        "id": mid,
        "parentId": None,
        "timestamp": "2026-11-05T11:57:30.265Z",
        "message": {"role": role, "content": content, "meta": {"source": source}},
    }


def test_project_slug_maps_slashes_to_dashes() -> None:
    assert fwd._project_slug("/home/alan") == "home-alan"
    assert fwd._project_slug("/home/alan/code/uw-dashboard") == "home-alan-code-uw-dashboard"


def test_discover_transcript_matches_workspace_and_newest(tmp_path: Path) -> None:
    _write_session(tmp_path, "/home/alan", "sess-other", [])
    want = _write_session(tmp_path, "/work/repo", "sess-want", [])
    found = fwd._discover_transcript(
        commandcode_home=tmp_path, workspace="/work/repo", launch_epoch_ms=0
    )
    assert found == want


def test_discover_transcript_skips_checkpoints(tmp_path: Path) -> None:
    path = _write_session(tmp_path, "/work/repo", "sess", [])
    path.with_name("sess.checkpoints.jsonl").write_text("{}\n", encoding="utf-8")
    found = fwd._discover_transcript(
        commandcode_home=tmp_path, workspace="/work/repo", launch_epoch_ms=0
    )
    assert found == path


def test_discover_transcript_rejects_transcript_older_than_launch(tmp_path: Path) -> None:
    """A transcript left by a prior ``cmd`` run must not be adopted.

    Reading it from offset 0 would re-mirror its whole history into the fresh
    conversation, so discovery waits for a file this launch produced.
    """
    stale = _write_session(tmp_path, "/work/repo", "sess-stale", [])
    stale_epoch_ms = 1_700_000_000_000
    os.utime(stale, (stale_epoch_ms / 1000, stale_epoch_ms / 1000))
    assert (
        fwd._discover_transcript(
            commandcode_home=tmp_path,
            workspace="/work/repo",
            launch_epoch_ms=stale_epoch_ms + 60_000,
        )
        is None
    )


def test_preseed_resume_state_positions_cursor_at_end(tmp_path: Path) -> None:
    entry = _msg("assistant", "model", [{"type": "text", "text": "earlier"}], "a0")
    entry["usage"] = {"inputTokens": 50, "outputTokens": 4, "costUsd": 0.002}
    entry["model"] = "deepseek/deepseek-v4.1-flash"
    transcript = _write_session(tmp_path, "/work/repo", "sess", [entry])
    bridge_dir = tmp_path / "bridge"
    assert fwd.preseed_commandcode_resume_state(
        bridge_dir,
        commandcode_home=tmp_path,
        workspace="/work/repo",
        external_session_id="sess",
    )

    state = fwd._read_state(bridge_dir)
    assert state.path == str(transcript)
    assert state.offset == transcript.stat().st_size
    # Cumulative usage carries over so the cost badge doesn't reset on relaunch.
    assert (state.input_tokens, state.output_tokens) == (50, 4)
    assert abs(state.cost_usd - 0.002) < 1e-9
    assert state.model == "deepseek/deepseek-v4.1-flash"
    # Nothing already in the transcript is re-mirrored.
    items, _usage, _, _ = fwd._read_new_items(transcript, state.offset, state.line)
    assert items == []
    # A turn appended after the preseed is the only thing mirrored.
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(_msg("user", "user", [{"type": "text", "text": "next"}], "u1")) + "\n"
        )
    items, _usage, _, _ = fwd._read_new_items(transcript, state.offset, state.line)
    assert [i.text for i in items] == ["next"]


def test_preseed_resume_state_missing_transcript_returns_false(tmp_path: Path) -> None:
    assert not fwd.preseed_commandcode_resume_state(
        tmp_path / "bridge",
        commandcode_home=tmp_path,
        workspace="/work/repo",
        external_session_id="missing",
    )


def test_read_new_items_maps_user_assistant_and_reasoning(tmp_path: Path) -> None:
    path = _write_session(
        tmp_path,
        "/work/repo",
        "sess",
        [
            _msg("user", "user", [{"type": "text", "text": "do the thing"}], "u1"),
            _msg(
                "assistant",
                "model",
                [
                    {"type": "thinking", "thinking": "pondering"},
                    {"type": "text", "text": "done"},
                    {"type": "tool_use", "id": "t1", "name": "shell_command", "input": {}},
                ],
                "a1",
            ),
            _msg(
                "user",
                "tool",
                [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "ok"}],
                    }
                ],
                "tr1",
            ),
        ],
    )
    items, _usage, offset, line = fwd._read_new_items(path, 0, 0)
    assert [(i.kind, i.role, i.text) for i in items] == [
        ("message", "user", "do the thing"),
        ("reasoning", "assistant", "pondering"),
        ("message", "assistant", "done"),
        ("function_call", "", ""),
        ("function_call_output", "", "ok"),
    ]
    call = items[3]
    assert (call.call_id, call.name) == ("t1", "shell_command")
    assert json.loads(call.arguments) == {}
    assert items[4].call_id == "t1"
    assert offset > 0 and line == 4

    # Re-reading from the new cursor yields nothing (idempotent).
    again, _usage2, offset2, _line = fwd._read_new_items(path, offset, line)
    assert again == []
    assert offset2 == offset


def test_read_new_items_holds_back_partial_trailing_line(tmp_path: Path) -> None:
    path = _write_session(tmp_path, "/work/repo", "sess", [])
    full = json.dumps(_msg("user", "user", [{"type": "text", "text": "hello"}], "u1"))
    half = len(full) // 2
    with path.open("a", encoding="utf-8") as handle:
        handle.write(full[:half])  # partial line, no trailing newline
    items, _u1, offset, _l1 = fwd._read_new_items(path, 0, 0)
    assert items == []
    # Completing the partial line surfaces the item on the next read.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(full[half:] + "\n")
    items, _u2, offset2, _l2 = fwd._read_new_items(path, offset, 1)
    assert [i.text for i in items] == ["hello"]
    assert offset2 > offset


def test_read_new_items_accumulates_usage_and_model(tmp_path: Path) -> None:
    entry = _msg("assistant", "model", [{"type": "text", "text": "hi"}], "a1")
    entry["usage"] = {
        "inputTokens": 100,
        "outputTokens": 7,
        "cacheReadTokens": 40,
        "cacheWriteTokens": 0,
        "costUsd": 0.01,
    }
    entry["model"] = "deepseek/deepseek-v4.1-flash"
    path = _write_session(tmp_path, "/work/repo", "sess", [entry])
    items, usage, _offset, _line = fwd._read_new_items(path, 0, 0)
    assert usage.advanced is True
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (100, 7, 40)
    assert usage.context_tokens == 100
    assert abs(usage.cost_usd - 0.01) < 1e-9
    assert usage.model == "deepseek/deepseek-v4.1-flash"
    assert [i.text for i in items] == ["hi"]


def test_clear_state_removes_cursor(tmp_path: Path) -> None:
    fwd._write_state(tmp_path, fwd._ForwardState(path="/x", offset=5, line=2))
    assert (tmp_path / fwd._STATE_FILE).exists()
    fwd.clear_commandcode_bridge_state(tmp_path)
    assert not (tmp_path / fwd._STATE_FILE).exists()
    # Clearing an absent cursor is a no-op.
    fwd.clear_commandcode_bridge_state(tmp_path)
