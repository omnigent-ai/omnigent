"""Unit tests for the kimi-native transcript forwarder.

Covers the pure parsing/discovery helpers against kimi's real ``wire.jsonl``
event schema (turn.prompt + content.part), the byte-offset state round-trip,
workspace/recency-based session discovery, and the forward loop's lifecycle
edges (failed finish reasons, pane death, quiescence, poison-line drops) with
the POST helpers stubbed out.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

import omnigent._native_forwarder_health as forwarder_health
import omnigent.kimi_native_forwarder as fwd
from omnigent.kimi_native_forwarder import (
    _discover_wire,
    _ForwardState,
    _offset_for_line,
    _read_state,
    _row_to_item,
    _write_state,
    clear_kimi_bridge_state,
    forward_kimi_wire_to_session,
    read_kimi_wire_items,
    read_new_kimi_wire_items,
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

    def test_think_part_is_reasoning(self) -> None:
        # Reasoning lives in part["think"] (not part["text"]) and is mirrored as a
        # reasoning item, not skipped — the kimi analogue of codex-native #1254.
        row = {
            "type": "context.append_loop_event",
            "event": {
                "type": "content.part",
                "uuid": "abc123",
                "part": {"type": "think", "think": "Let me reason about this."},
            },
        }
        item = _row_to_item(5, row)
        assert item is not None
        assert item.kind == "reasoning"
        assert item.role == "assistant"
        assert item.text == "Let me reason about this."
        assert item.response_id == "kimi:abc123"

    def test_tool_call_and_metadata_skipped(self) -> None:
        for row in (
            {"type": "context.append_loop_event", "event": {"type": "tool.call", "name": "Read"}},
            {"type": "metadata", "protocol_version": 1},
            {"type": "usage.record", "usage": {}},
            {"type": "context.append_message", "message": {"role": "user", "content": []}},
        ):
            assert _row_to_item(0, row) is None

    def test_step_end_with_end_turn_is_turn_end(self) -> None:
        """``end_turn`` is the edge that reports terminal status to the parent."""
        row = {
            "type": "context.append_loop_event",
            "event": {
                "type": "step.end",
                "turnId": "0",
                "step": 3,
                "finishReason": "end_turn",
            },
        }
        item = _row_to_item(28, row)
        assert item is not None
        assert item.kind == "turn_end"
        assert item.response_id == "kimi:turn_end:28"

    def test_step_end_with_tool_use_is_skipped(self) -> None:
        """A step that stopped to call a tool is mid-turn, not a completion."""
        row = {
            "type": "context.append_loop_event",
            "event": {
                "type": "step.end",
                "turnId": "0",
                "step": 1,
                "finishReason": "tool_use",
            },
        }
        assert _row_to_item(12, row) is None

    def test_step_end_with_failure_reason_is_turn_failed(self) -> None:
        """error/abort steps end the turn abnormally → a failed edge, not a strand."""
        for reason in ("error", "abort", "aborted"):
            row = {
                "type": "context.append_loop_event",
                "event": {"type": "step.end", "turnId": "0", "step": 2, "finishReason": reason},
            }
            item = _row_to_item(30, row)
            assert item is not None, reason
            assert item.kind == "turn_failed"
            assert item.response_id == "kimi:turn_failed:30"

    def test_step_end_with_length_is_turn_end(self) -> None:
        """A length-stopped step delivered (truncated) output — an idle edge."""
        row = {
            "type": "context.append_loop_event",
            "event": {"type": "step.end", "turnId": "0", "step": 2, "finishReason": "length"},
        }
        item = _row_to_item(31, row)
        assert item is not None
        assert item.kind == "turn_end"

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
        items = read_kimi_wire_items(self._wire(tmp_path), 0)
        assert [(i.role, i.text) for i in items] == [("user", "hi"), ("assistant", "hello!")]

    def test_offset_skips_already_seen(self, tmp_path: Path) -> None:
        wire = self._wire(tmp_path)
        # last_line past the user prompt (line 1) → only the assistant text (line 3).
        items = read_kimi_wire_items(wire, 2)
        assert [(i.role, i.text) for i in items] == [("assistant", "hello!")]
        assert items[0].line_no == 3

    def test_missing_file_is_empty(self, tmp_path: Path) -> None:
        assert read_kimi_wire_items(tmp_path / "nope.jsonl", 0) == []


class TestState:
    def test_round_trip_and_clear(self, tmp_path: Path) -> None:
        assert _read_state(tmp_path) is None
        _write_state(tmp_path, _ForwardState(wire_path="/x/wire.jsonl", last_line=7, offset=345))
        loaded = _read_state(tmp_path)
        assert loaded is not None
        assert loaded.wire_path == "/x/wire.jsonl"
        assert loaded.last_line == 7
        assert loaded.offset == 345
        clear_kimi_bridge_state(tmp_path)
        assert _read_state(tmp_path) is None

    def test_legacy_state_without_offset_marks_unknown(self, tmp_path: Path) -> None:
        # State written by a line-only build: offset -1 → re-derived on first poll.
        (tmp_path / "kimi_forwarder.json").write_text(
            json.dumps({"wire_path": "/x/wire.jsonl", "last_line": 3}), encoding="utf-8"
        )
        loaded = _read_state(tmp_path)
        assert loaded is not None
        assert loaded.offset == -1


class TestReadNewIncremental:
    def _write_rows(self, path: Path, rows: list[dict[str, object]], *, partial: str = "") -> None:
        text = "".join(json.dumps(r) + "\n" for r in rows) + partial
        path.write_text(text, encoding="utf-8")

    def _prompt(self, text: str) -> dict[str, object]:
        return {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": text}],
            "origin": {"kind": "user"},
        }

    def test_reads_only_past_offset_and_resumes(self, tmp_path: Path) -> None:
        wire = tmp_path / "wire.jsonl"
        self._write_rows(wire, [{"type": "metadata"}, self._prompt("hi")])
        items, offset, line = read_new_kimi_wire_items(wire, 0, 0)
        assert [(i.role, i.text) for i in items] == [("user", "hi")]
        assert offset == wire.stat().st_size
        assert line == 2
        assert items[0].offset_after == offset
        # Append one more row; the next read starts at the persisted cursor.
        with wire.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(self._prompt("again")) + "\n")
        items, offset2, line2 = read_new_kimi_wire_items(wire, offset, line)
        assert [(i.text, i.line_no) for i in items] == [("again", 2)]
        assert offset2 == wire.stat().st_size
        assert line2 == 3

    def test_partial_trailing_line_left_for_next_poll(self, tmp_path: Path) -> None:
        wire = tmp_path / "wire.jsonl"
        self._write_rows(wire, [self._prompt("hi")], partial='{"type": "turn.pro')
        items, offset, line = read_new_kimi_wire_items(wire, 0, 0)
        assert len(items) == 1
        assert offset < wire.stat().st_size
        assert line == 1

    def test_truncated_file_restarts_tail(self, tmp_path: Path) -> None:
        wire = tmp_path / "wire.jsonl"
        self._write_rows(wire, [self._prompt("fresh")])
        items, offset, line = read_new_kimi_wire_items(wire, 10_000, 42)
        assert [(i.text, i.line_no) for i in items] == [("fresh", 0)]
        assert offset == wire.stat().st_size
        assert line == 1

    def test_missing_file_is_noop(self, tmp_path: Path) -> None:
        assert read_new_kimi_wire_items(tmp_path / "nope.jsonl", 5, 1) == ([], 5, 1)

    def test_offset_for_line_matches_line_starts(self, tmp_path: Path) -> None:
        wire = tmp_path / "wire.jsonl"
        wire.write_text('{"a":1}\n{"b":2}\n{"c":3}\n', encoding="utf-8")
        assert _offset_for_line(wire, 0) == 0
        assert _offset_for_line(wire, 1) == 8
        assert _offset_for_line(wire, 3) == 24
        assert _offset_for_line(wire, 99) == 24


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


def _wire_home(tmp_path: Path, rows: list[dict[str, object]]) -> tuple[Path, Path]:
    """Build a kimi home holding one discoverable session wire with *rows*."""
    home = tmp_path / "kimi-code-home"
    wire = home / "sessions" / "wd_x" / "session_a" / "agents" / "main" / "wire.jsonl"
    wire.parent.mkdir(parents=True)
    wire.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return home, wire


def _prompt_row(text: str = "go") -> dict[str, object]:
    return {
        "type": "turn.prompt",
        "input": [{"type": "text", "text": text}],
        "origin": {"kind": "user"},
    }


def _assistant_row(uuid: str, text: str) -> dict[str, object]:
    return {
        "type": "context.append_loop_event",
        "event": {"type": "content.part", "uuid": uuid, "part": {"type": "text", "text": text}},
    }


def _step_end_row(reason: str) -> dict[str, object]:
    return {
        "type": "context.append_loop_event",
        "event": {"type": "step.end", "turnId": "0", "step": 1, "finishReason": reason},
    }


async def _drive_loop_until(
    tmp_path: Path,
    rows: list[dict[str, object]],
    done: Callable[[], bool],
    *,
    pane_alive: Callable[[], bool] | None = None,
    quiescence_s: float = 60.0,
) -> None:
    """Run the forward loop against a canned wire until *done* (then cancel)."""
    bridge = tmp_path / "bridge"
    bridge.mkdir(exist_ok=True)
    home, _wire = _wire_home(tmp_path, rows)
    task = asyncio.create_task(
        forward_kimi_wire_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_k",
            bridge_dir=bridge,
            kimi_home=home,
            workspace="/ws",
            launch_epoch_ms=0,
            pane_alive=pane_alive,
            quiescence_s=quiescence_s,
            poll_interval_s=0.01,
        )
    )
    try:
        for _ in range(300):
            if done():
                return
            await asyncio.sleep(0.01)
        raise AssertionError("forward loop never reached the expected state")
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestForwardLoopEdges:
    """Lifecycle edges of the forward loop, with the POST helpers stubbed."""

    @pytest.fixture(autouse=True)
    def _stub_posts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.items: list[str] = []
        self.statuses: list[tuple[str, str]] = []
        forwarder_health.clear()

        async def _fake_item(_client: object, **kwargs: object) -> None:
            self.items.append(kwargs["item"].response_id)  # type: ignore[union-attr]

        async def _fake_status(_client: object, **kwargs: object) -> None:
            self.statuses.append((str(kwargs["status"]), str(kwargs["output"])))

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)
        monkeypatch.setattr(fwd, "_post_reasoning_item", _fake_item)
        monkeypatch.setattr(fwd, "_post_external_session_status", _fake_status)

    async def test_error_finish_reason_posts_failed_edge(self, tmp_path: Path) -> None:
        rows = [_prompt_row(), _assistant_row("u1", "partial"), _step_end_row("error")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses))
        assert self.statuses == [("failed", "partial")]

    async def test_end_turn_posts_idle_edge_with_output(self, tmp_path: Path) -> None:
        rows = [_prompt_row(), _assistant_row("u1", "done!"), _step_end_row("end_turn")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses))
        assert self.statuses == [("idle", "done!")]

    async def test_pane_death_mid_turn_posts_failed_edge(self, tmp_path: Path) -> None:
        # A prompt with no terminal edge and a dead pane: fail instead of strand.
        rows = [_prompt_row(), _assistant_row("u1", "so far")]
        await _drive_loop_until(
            tmp_path, rows, lambda: bool(self.statuses), pane_alive=lambda: False
        )
        assert self.statuses == [("failed", "so far")]

    async def test_quiescence_closes_turn_without_edge_as_idle(self, tmp_path: Path) -> None:
        # An interrupted turn writes no wire edge; the quiet-wire fallback closes it.
        rows = [_prompt_row(), _assistant_row("u1", "so far")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses), quiescence_s=0.05)
        assert self.statuses == [("idle", "so far")]


class TestForwardLoopPostFailures:
    async def test_poison_item_dropped_after_bounded_retries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 4xx-rejected item is skipped after bounded retries; the tail moves on."""
        posted: list[str] = []
        statuses: list[str] = []
        rejections = 0

        async def _fake_item(_client: object, **kwargs: object) -> None:
            response_id = kwargs["item"].response_id  # type: ignore[union-attr]
            if response_id == "kimi:turn:0":
                nonlocal rejections
                rejections += 1
                request = httpx.Request("POST", "http://test")
                raise httpx.HTTPStatusError(
                    "422", request=request, response=httpx.Response(422, request=request)
                )
            posted.append(str(response_id))

        async def _fake_status(_client: object, **kwargs: object) -> None:
            statuses.append(str(kwargs["status"]))

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)
        monkeypatch.setattr(fwd, "_post_external_session_status", _fake_status)

        rows = [_prompt_row("poison"), _assistant_row("u1", "ok"), _step_end_row("end_turn")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(statuses))
        assert rejections == 3
        assert posted == ["kimi:u1"]
        assert statuses == ["idle"]

    async def test_transport_failure_is_recorded_and_retried(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A connect failure attributes itself to forwarder health and holds the cursor."""
        forwarder_health.clear()
        attempts = 0

        async def _fake_item(_client: object, **kwargs: object) -> None:
            nonlocal attempts
            attempts += 1
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)

        await _drive_loop_until(tmp_path, [_prompt_row()], lambda: attempts >= 3)
        assert forwarder_health.recent_post_failure(60.0) is not None
        forwarder_health.clear()
