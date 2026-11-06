"""Unit tests for the commandcode-native transcript forwarder."""

from __future__ import annotations

import json
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
    items, offset, line = fwd._read_new_items(path, 0, 0)
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
    again, offset2, _ = fwd._read_new_items(path, offset, line)
    assert again == []
    assert offset2 == offset


def test_read_new_items_holds_back_partial_trailing_line(tmp_path: Path) -> None:
    path = _write_session(tmp_path, "/work/repo", "sess", [])
    full = json.dumps(_msg("user", "user", [{"type": "text", "text": "hello"}], "u1"))
    half = len(full) // 2
    with path.open("a", encoding="utf-8") as handle:
        handle.write(full[:half])  # partial line, no trailing newline
    items, offset, _ = fwd._read_new_items(path, 0, 0)
    assert items == []
    # Completing the partial line surfaces the item on the next read.
    with path.open("a", encoding="utf-8") as handle:
        handle.write(full[half:] + "\n")
    items, offset2, _ = fwd._read_new_items(path, offset, 1)
    assert [i.text for i in items] == ["hello"]
    assert offset2 > offset


def test_clear_state_removes_cursor(tmp_path: Path) -> None:
    fwd._write_state(tmp_path, fwd._ForwardState(path="/x", offset=5, line=2))
    assert (tmp_path / fwd._STATE_FILE).exists()
    fwd.clear_commandcode_bridge_state(tmp_path)
    assert not (tmp_path / fwd._STATE_FILE).exists()
    # Clearing an absent cursor is a no-op.
    fwd.clear_commandcode_bridge_state(tmp_path)
