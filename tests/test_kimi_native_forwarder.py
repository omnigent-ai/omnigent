"""Unit tests for the kimi-native transcript forwarder.

Covers the pure parsing/discovery helpers against kimi's real ``wire.jsonl``
event schema (turn.prompt + content.part), the line-offset state round-trip,
and workspace/recency-based session discovery. The live POST loop is exercised
by the e2e gate, not here.
"""

from __future__ import annotations

import json
from pathlib import Path

from omnigent.kimi_native_forwarder import (
    _discover_wire,
    _ForwardState,
    _read_new_items,
    _read_state,
    _row_to_item,
    _write_state,
    clear_kimi_bridge_state,
)


class TestRowToItem:
    def test_turn_prompt_is_user(self) -> None:
        row = {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": "what is in this repo?"}],
            "origin": {"kind": "user"},
        }
        item = _row_to_item(4, row)
        assert item is not None
        assert item.role == "user"
        assert item.text == "what is in this repo?"
        assert item.response_id == "kimi:turn:4"

    def test_content_part_text_is_assistant(self) -> None:
        row = {
            "type": "context.append_loop_event",
            "event": {
                "type": "content.part",
                "uuid": "67ce67f7",
                "part": {"type": "text", "text": "This is **Omnigent**."},
            },
        }
        item = _row_to_item(9, row)
        assert item is not None
        assert item.role == "assistant"
        assert item.text == "This is **Omnigent**."
        assert item.response_id == "kimi:67ce67f7"

    def test_think_part_is_skipped(self) -> None:
        row = {
            "type": "context.append_loop_event",
            "event": {"type": "content.part", "part": {"type": "think", "think": "reasoning"}},
        }
        assert _row_to_item(5, row) is None

    def test_tool_call_and_metadata_skipped(self) -> None:
        for row in (
            {"type": "context.append_loop_event", "event": {"type": "tool.call", "name": "Read"}},
            {"type": "metadata", "protocol_version": 1},
            {"type": "usage.record", "usage": {}},
            {"type": "context.append_message", "message": {"role": "user", "content": []}},
        ):
            assert _row_to_item(0, row) is None

    def test_non_user_turn_prompt_skipped(self) -> None:
        row = {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": "x"}],
            "origin": {"kind": "system"},
        }
        assert _row_to_item(0, row) is None


class TestReadNewItems:
    def _wire(self, tmp_path: Path) -> Path:
        def _part(uuid: str, part_type: str, text: str) -> dict[str, object]:
            return {
                "type": "context.append_loop_event",
                "event": {
                    "type": "content.part",
                    "uuid": uuid,
                    "part": {"type": part_type, "text": text},
                },
            }

        rows = [
            {"type": "metadata", "protocol_version": 1},
            {
                "type": "turn.prompt",
                "input": [{"type": "text", "text": "hi"}],
                "origin": {"kind": "user"},
            },
            _part("u1", "think", "…"),
            _part("u2", "text", "hello!"),
        ]
        p = tmp_path / "wire.jsonl"
        p.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        return p

    def test_parses_user_and_assistant_only(self, tmp_path: Path) -> None:
        items = _read_new_items(self._wire(tmp_path), 0)
        assert [(i.role, i.text) for i in items] == [("user", "hi"), ("assistant", "hello!")]

    def test_offset_skips_already_seen(self, tmp_path: Path) -> None:
        wire = self._wire(tmp_path)
        # last_line past the user prompt (line 1) → only the assistant text (line 3).
        items = _read_new_items(wire, 2)
        assert [(i.role, i.text) for i in items] == [("assistant", "hello!")]
        assert items[0].line_no == 3

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert _read_new_items(tmp_path / "nope.jsonl", 0) == []


class TestState:
    def test_round_trip_and_clear(self, tmp_path: Path) -> None:
        assert _read_state(tmp_path) is None
        _write_state(tmp_path, _ForwardState(wire_path="/x/wire.jsonl", last_line=7))
        loaded = _read_state(tmp_path)
        assert loaded is not None
        assert loaded.wire_path == "/x/wire.jsonl"
        assert loaded.last_line == 7
        clear_kimi_bridge_state(tmp_path)
        assert _read_state(tmp_path) is None


class TestDiscoverWire:
    def _make_session(
        self, home: Path, session_dir_name: str, work_dir: str, *, mtime: float
    ) -> Path:
        wire = home / "sessions" / "wd_x" / session_dir_name / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True, exist_ok=True)
        wire.write_text("{}\n", encoding="utf-8")
        import os

        os.utime(wire, (mtime, mtime))
        # session_index keys on the session dir (…/<wd_…>/<session_…>).
        idx = home / "session_index.jsonl"
        index_row = {"sessionDir": str(wire.parent.parent.parent), "workDir": work_dir}
        with idx.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(index_row) + "\n")
        return wire

    def test_picks_newest_matching_workspace(self, tmp_path: Path) -> None:
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        self._make_session(home, "session_old", "/ws", mtime=1000.0)
        newest = self._make_session(home, "session_new", "/ws", mtime=2000.0)
        self._make_session(home, "session_other", "/different", mtime=3000.0)
        found = _discover_wire(home, "/ws", launch_epoch_ms=0)
        assert found == newest

    def test_none_before_any_session(self, tmp_path: Path) -> None:
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        assert _discover_wire(home, "/ws", launch_epoch_ms=0) is None

    def test_ignores_sessions_before_launch(self, tmp_path: Path) -> None:
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        self._make_session(home, "session_stale", "/ws", mtime=1000.0)
        # launch far in the future (ms) → the 1000s-mtime session is below the floor.
        assert _discover_wire(home, "/ws", launch_epoch_ms=9_000_000_000_000) is None
