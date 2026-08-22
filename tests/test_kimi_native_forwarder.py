"""Unit tests for the kimi-native transcript forwarder.

Covers the pure parsing/discovery helpers against kimi's real ``wire.jsonl``
event schema (including the sanitized real-session fixtures under
``tests/fixtures/kimi_wire``), the byte-offset state round-trip, strict
launch-epoch session discovery, and the forward loop's lifecycle edges
(``turn.ended`` records, pane death, quiescence with in-flight tool
suppression, transient-vs-poison POST rejections, edge dedupe across
restarts) with the POST helpers stubbed out.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

import omnigent._native_forwarder_health as forwarder_health
import omnigent.kimi_native_forwarder as fwd
from omnigent.kimi_native_forwarder import (
    _discover_wire,
    _ForwardState,
    _read_state,
    _row_to_item,
    _write_state,
    clear_kimi_bridge_state,
    forward_kimi_wire_to_session,
    read_kimi_wire_items,
    read_new_kimi_wire_items,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "kimi_wire"


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

    def test_metadata_rows_skipped(self) -> None:
        for row in (
            {"type": "metadata", "protocol_version": 1},
            {"type": "usage.record", "usage": {}},
            {"type": "context.append_message", "message": {"role": "user", "content": []}},
            {"type": "turn.cancel", "turnId": 0, "reason": "user_cancelled"},
        ):
            assert _row_to_item(0, row) is None

    def test_tool_call_and_result_are_bookkeeping_items(self) -> None:
        """Tool events are never posted but tracked for the quiescence gate."""
        call = {
            "type": "context.append_loop_event",
            "event": {"type": "tool.call", "uuid": "t1", "name": "Read"},
        }
        result = {
            "type": "context.append_loop_event",
            "event": {"type": "tool.result", "parentUuid": "t1"},
        }
        call_item = _row_to_item(7, call)
        result_item = _row_to_item(8, result)
        assert call_item is not None and call_item.kind == "tool_call"
        assert result_item is not None and result_item.kind == "tool_result"

    def test_step_end_never_drives_a_turn_edge(self) -> None:
        """Edges come from turn.ended: failed/cancelled turns write no step.end."""
        for reason in ("end_turn", "length", "tool_use", "error", "abort"):
            row = {
                "type": "context.append_loop_event",
                "event": {"type": "step.end", "turnId": "0", "step": 1, "finishReason": reason},
            }
            assert _row_to_item(16, row) is None, reason

    def test_turn_ended_completed_is_turn_end(self) -> None:
        row = {"type": "turn.ended", "turnId": 0, "reason": "completed", "durationMs": 174932}
        item = _row_to_item(31, row)
        assert item is not None
        assert item.kind == "turn_end"
        assert item.response_id == "kimi:turn_end:31"

    def test_turn_ended_cancelled_is_turn_end(self) -> None:
        row = {"type": "turn.ended", "turnId": 0, "reason": "cancelled", "durationMs": 1112}
        item = _row_to_item(10, row)
        assert item is not None
        assert item.kind == "turn_end"

    def test_turn_ended_failed_is_turn_failed_with_error_message(self) -> None:
        row = {
            "type": "turn.ended",
            "turnId": 0,
            "reason": "failed",
            "error": {"code": "provider.auth_error", "message": "401 Invalid Token"},
        }
        item = _row_to_item(9, row)
        assert item is not None
        assert item.kind == "turn_failed"
        assert item.text == "401 Invalid Token"
        assert item.response_id == "kimi:turn_failed:9"

    def test_turn_ended_unknown_reason_keeps_turn_open(self) -> None:
        """Unknown vocabulary never fails open to 'failed'; the fallbacks close it."""
        row = {"type": "turn.ended", "turnId": 0, "reason": "paused"}
        assert _row_to_item(9, row) is None

    def test_non_user_turn_prompt_skipped(self) -> None:
        row = {
            "type": "turn.prompt",
            "input": [{"type": "text", "text": "x"}],
            "origin": {"kind": "system"},
        }
        assert _row_to_item(0, row) is None


class TestRealWireFixtures:
    """Sanitized real kimi 0.34 wire logs are the ground truth for turn edges."""

    def test_completed_turn_ends_with_single_idle_edge(self) -> None:
        items = read_kimi_wire_items(_FIXTURES / "completed.jsonl", 0)
        kinds = [i.kind for i in items]
        assert kinds[-1] == "turn_end"
        assert kinds.count("turn_end") == 1
        assert "turn_failed" not in kinds
        assert kinds.count("tool_call") == kinds.count("tool_result") == 3

    def test_failed_turn_emits_failed_edge_without_any_step_end(self) -> None:
        items = read_kimi_wire_items(_FIXTURES / "failed.jsonl", 0)
        kinds = [i.kind for i in items]
        assert kinds == ["message", "turn_failed"]
        assert items[0].role == "user"
        assert items[1].text == "401 Invalid Token"

    def test_cancelled_turns_each_emit_an_idle_edge(self) -> None:
        items = read_kimi_wire_items(_FIXTURES / "cancelled.jsonl", 0)
        kinds = [i.kind for i in items]
        assert kinds.count("turn_end") == 2
        assert "turn_failed" not in kinds
        assert sum(1 for i in items if i.kind == "message" and i.role == "user") == 2


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
        _write_state(
            tmp_path,
            _ForwardState(
                wire_path="/x/wire.jsonl",
                last_line=7,
                offset=345,
                turn_open=True,
                tools_in_flight=2,
                last_edge_id="kimi:turn_end:5",
                dropped_edge_status="failed",
                last_activity_ts=1_700_000_000.5,
                last_seen_offset=400,
            ),
        )
        loaded = _read_state(tmp_path)
        assert loaded is not None
        assert loaded.wire_path == "/x/wire.jsonl"
        assert loaded.last_line == 7
        assert loaded.offset == 345
        assert loaded.turn_open is True
        assert loaded.tools_in_flight == 2
        assert loaded.last_edge_id == "kimi:turn_end:5"
        assert loaded.dropped_edge_status == "failed"
        assert loaded.last_activity_ts == 1_700_000_000.5
        assert loaded.last_seen_offset == 400
        clear_kimi_bridge_state(tmp_path)
        assert _read_state(tmp_path) is None

    def test_state_without_offset_is_discarded(self, tmp_path: Path) -> None:
        # No shipped build ever wrote line-only state; treat it as no state at
        # all so the tail restarts cleanly instead of migrating.
        (tmp_path / "kimi_forwarder.json").write_text(
            json.dumps({"wire_path": "/x/wire.jsonl", "last_line": 3}), encoding="utf-8"
        )
        assert _read_state(tmp_path) is None

    def test_lifecycle_fields_default_when_absent(self, tmp_path: Path) -> None:
        (tmp_path / "kimi_forwarder.json").write_text(
            json.dumps({"wire_path": "/x/wire.jsonl", "last_line": 3, "offset": 10}),
            encoding="utf-8",
        )
        loaded = _read_state(tmp_path)
        assert loaded is not None
        assert loaded.turn_open is False
        assert loaded.tools_in_flight == 0
        assert loaded.last_edge_id is None


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


class TestDiscoverWire:
    def _make_session(
        self,
        home: Path,
        session_dir_name: str,
        work_dir: str,
        *,
        mtime: float,
        first_row: str = "{}",
    ) -> Path:
        wire = home / "sessions" / "wd_x" / session_dir_name / "agents" / "main" / "wire.jsonl"
        wire.parent.mkdir(parents=True, exist_ok=True)
        wire.write_text(first_row + "\n", encoding="utf-8")
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

    def test_wire_just_before_launch_is_not_adopted(self, tmp_path: Path) -> None:
        """Adoption pins strictly to the launch epoch — no skew window."""
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        self._make_session(home, "session_prior", "/ws", mtime=9_995.0)
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_000) is None

    def test_subsecond_wire_before_launch_ms_is_not_adopted(self, tmp_path: Path) -> None:
        """A precise mtime 400ms before launch is a prior launch's wire, not ours."""
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        self._make_session(home, "session_prior", "/ws", mtime=10_000.2)
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_600) is None

    def test_precise_wire_after_launch_ms_is_adopted(self, tmp_path: Path) -> None:
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        wire = self._make_session(home, "session_fresh", "/ws", mtime=10_000.75)
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_600) == wire

    def test_whole_second_mtime_within_launch_second_is_adopted(self, tmp_path: Path) -> None:
        """A second-truncating filesystem only needs to reach the launch second
        when the wire content offers no timestamp to break the tie."""
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        wire = self._make_session(home, "session_fresh", "/ws", mtime=10_000.0)
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_600) == wire

    def test_coarse_tie_rejected_when_header_predates_launch(self, tmp_path: Path) -> None:
        """A same-second truncated mtime defers to the wire's metadata header."""
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        header = json.dumps({"type": "metadata", "created_at": 10_000_200})
        self._make_session(home, "session_prior", "/ws", mtime=10_000.0, first_row=header)
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_600) is None

    def test_coarse_tie_adopted_when_header_postdates_launch(self, tmp_path: Path) -> None:
        home = tmp_path / "kimi-code-home"
        home.mkdir()
        header = json.dumps({"type": "metadata", "created_at": 10_000_650})
        wire = self._make_session(home, "session_fresh", "/ws", mtime=10_000.0, first_row=header)
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_600) == wire

    def test_coarse_tie_deferred_while_header_mid_write(self, tmp_path: Path) -> None:
        """A partial/unparseable first line defers adoption to the next poll —
        it must not fall through to the coarse mtime floor."""
        import os

        home = tmp_path / "kimi-code-home"
        home.mkdir()
        wire = self._make_session(home, "session_fresh", "/ws", mtime=10_000.0)
        # Header truncated mid-write (no trailing newline yet).
        wire.write_text('{"type": "metadata", "created_', encoding="utf-8")
        os.utime(wire, (10_000.0, 10_000.0))
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_600) is None
        # A complete-but-broken line is equally untrustworthy.
        wire.write_text('{"broken json}\n', encoding="utf-8")
        os.utime(wire, (10_000.0, 10_000.0))
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_600) is None
        # Next poll the header is complete: the timestamp decides.
        header = json.dumps({"type": "metadata", "created_at": 10_000_650})
        wire.write_text(header + "\n", encoding="utf-8")
        os.utime(wire, (10_000.0, 10_000.0))
        assert _discover_wire(home, "/ws", launch_epoch_ms=10_000_600) == wire


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


def _tool_call_row(uuid: str = "t1") -> dict[str, object]:
    return {
        "type": "context.append_loop_event",
        "event": {"type": "tool.call", "uuid": uuid, "turnId": "0", "name": "Bash"},
    }


def _turn_ended_row(reason: str, *, error_message: str | None = None) -> dict[str, object]:
    row: dict[str, object] = {"type": "turn.ended", "turnId": 0, "reason": reason}
    if error_message is not None:
        row["error"] = {"code": "provider.auth_error", "message": error_message}
    return row


async def _drive_loop_until(
    tmp_path: Path,
    rows: list[dict[str, object]],
    done: Callable[[], bool],
    *,
    pane_alive: Callable[[], bool] | None = None,
    quiescence_s: float = 60.0,
    tool_quiescence_s: float = 60.0,
    prepare: Callable[[Path, Path], None] | None = None,
) -> None:
    """Run the forward loop against a canned wire until *done* (then cancel).

    *prepare* runs with ``(bridge_dir, wire_path)`` before the loop starts, for
    tests that seed persisted forwarder state.
    """
    bridge = tmp_path / "bridge"
    bridge.mkdir(exist_ok=True)
    home, wire = _wire_home(tmp_path, rows)
    if prepare is not None:
        prepare(bridge, wire)
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
            tool_quiescence_s=tool_quiescence_s,
            poll_interval_s=0.01,
            post_backoff_initial_s=0.01,
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

    async def test_turn_ended_failed_posts_failed_edge(self, tmp_path: Path) -> None:
        rows = [_prompt_row(), _assistant_row("u1", "partial"), _turn_ended_row("failed")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses))
        assert self.statuses == [("failed", "partial")]

    async def test_failed_turn_without_output_surfaces_provider_error(
        self, tmp_path: Path
    ) -> None:
        # The failed fixture shape: prompt → turn.ended(failed), no assistant text.
        rows = [_prompt_row(), _turn_ended_row("failed", error_message="401 Invalid Token")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses))
        assert self.statuses == [("failed", "401 Invalid Token")]

    async def test_turn_ended_completed_posts_idle_edge_with_output(self, tmp_path: Path) -> None:
        rows = [_prompt_row(), _assistant_row("u1", "done!"), _turn_ended_row("completed")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses))
        assert self.statuses == [("idle", "done!")]

    async def test_turn_ended_cancelled_posts_idle_edge(self, tmp_path: Path) -> None:
        rows = [_prompt_row(), _turn_ended_row("cancelled")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses))
        assert self.statuses == [("idle", "")]

    async def test_pane_death_mid_turn_posts_failed_edge(self, tmp_path: Path) -> None:
        # A prompt with no terminal edge and a dead pane: fail instead of strand.
        rows = [_prompt_row(), _assistant_row("u1", "so far")]
        await _drive_loop_until(
            tmp_path, rows, lambda: bool(self.statuses), pane_alive=lambda: False
        )
        assert self.statuses == [("failed", "so far")]

    async def test_quiescence_closes_turn_without_edge_as_idle(self, tmp_path: Path) -> None:
        # A wedged turn writes no wire edge; the quiet-wire fallback closes it.
        rows = [_prompt_row(), _assistant_row("u1", "so far")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses), quiescence_s=0.05)
        assert self.statuses == [("idle", "so far")]

    async def test_quiescence_suppressed_while_tool_in_flight(self, tmp_path: Path) -> None:
        """A silent wire mid-tool is a long tool call; only pane death closes it."""
        pane_up = True
        rows = [_prompt_row(), _tool_call_row()]
        checks = 0

        def _done() -> bool:
            nonlocal pane_up, checks
            if self.statuses:
                return True
            checks += 1
            # 20 checks ≈ 200ms with a 20ms quiescence window: staying edge-free
            # this long proves suppression; then a pane death must close it.
            if checks == 20:
                pane_up = False
            return False

        await _drive_loop_until(
            tmp_path,
            rows,
            _done,
            pane_alive=lambda: pane_up,
            quiescence_s=0.02,
        )
        assert self.statuses == [("failed", "")]

    async def test_hung_tool_watchdog_fails_turn_after_ceiling(self, tmp_path: Path) -> None:
        """Crash-mid-tool replay: the real completed-turn wire cut right after its
        first tool.call, then eternal silence in an alive pane. Suppression must
        not last forever — the tool-quiescence ceiling fails the turn."""
        fixture_rows = [
            json.loads(line)
            for line in (_FIXTURES / "completed.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        cut = next(
            i
            for i, row in enumerate(fixture_rows)
            if row.get("type") == "context.append_loop_event"
            and row.get("event", {}).get("type") == "tool.call"
        )
        rows = fixture_rows[: cut + 1]
        await _drive_loop_until(
            tmp_path,
            rows,
            lambda: bool(self.statuses),
            pane_alive=lambda: True,
            quiescence_s=0.02,
            tool_quiescence_s=0.1,
        )
        assert self.statuses == [("failed", "sanitized text part")]

    async def test_readopted_wire_ignores_stale_edge_dedupe(self, tmp_path: Path) -> None:
        """A newly discovered wire restarts line numbering; a stale edge id from
        the prior wire must not swallow the new wire's edge at the same line."""
        rows = [_prompt_row(), _turn_ended_row("completed")]

        def _seed(bridge: Path, wire: Path) -> None:
            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire.parent / "gone.jsonl"),
                    last_line=5,
                    offset=999,
                    last_edge_id="kimi:turn_end:1",
                ),
            )

        await _drive_loop_until(tmp_path, rows, lambda: bool(self.statuses), prepare=_seed)
        assert self.statuses == [("idle", "")]

    async def test_turn_open_persists_across_restart(self, tmp_path: Path) -> None:
        """A restarted forwarder still fails a stranded turn it never observed."""
        rows = [_prompt_row(), _assistant_row("u1", "so far")]

        def _seed(bridge: Path, wire: Path) -> None:
            # State a prior forwarder persisted mid-turn: wire fully consumed,
            # turn still open.
            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=2,
                    offset=wire.stat().st_size,
                    turn_open=True,
                ),
            )

        await _drive_loop_until(
            tmp_path,
            rows,
            lambda: bool(self.statuses),
            pane_alive=lambda: False,
            prepare=_seed,
        )
        assert self.statuses == [("failed", "")]
        assert self.items == []

    async def test_dropped_edge_status_persists_across_restart(self, tmp_path: Path) -> None:
        """A restart must not soften a poison-dropped failure back to idle."""
        rows = [_prompt_row()]

        def _seed(bridge: Path, wire: Path) -> None:
            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=1,
                    offset=wire.stat().st_size,
                    turn_open=True,
                    dropped_edge_status="failed",
                ),
            )

        await _drive_loop_until(
            tmp_path, rows, lambda: bool(self.statuses), quiescence_s=0.05, prepare=_seed
        )
        assert self.statuses == [("failed", "")]

    async def test_tool_silence_clock_survives_restart(self, tmp_path: Path) -> None:
        """A restart resumes the tool-quiescence window instead of re-arming it,
        so a crash-looping forwarder still fires the hung-tool watchdog."""
        rows = [_prompt_row(), _tool_call_row()]

        def _seed(bridge: Path, wire: Path) -> None:
            import time as _time

            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=2,
                    offset=wire.stat().st_size,
                    turn_open=True,
                    tools_in_flight=1,
                    last_activity_ts=_time.time() - 100.0,
                ),
            )

        await _drive_loop_until(
            tmp_path,
            rows,
            lambda: bool(self.statuses),
            pane_alive=lambda: True,
            quiescence_s=200.0,
            tool_quiescence_s=50.0,
            prepare=_seed,
        )
        assert self.statuses == [("failed", "")]

    async def test_wall_clock_jump_gets_restart_grace(self, tmp_path: Path) -> None:
        """A huge apparent elapsed silence (sleep/wake, NTP step) must not fire
        a watchdog on the spot — the restart grace gives the wire a moment."""
        rows = [_prompt_row()]

        def _seed(bridge: Path, wire: Path) -> None:
            size = wire.stat().st_size
            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=1,
                    offset=size,
                    turn_open=True,
                    last_activity_ts=time.time() - 10_000.0,
                    last_seen_offset=size,
                ),
            )

        started = time.monotonic()
        await _drive_loop_until(
            tmp_path, rows, lambda: bool(self.statuses), quiescence_s=5.0, prepare=_seed
        )
        assert self.statuses == [("idle", "")]
        assert time.monotonic() - started >= 0.8

    async def test_near_expired_window_still_fires_promptly(self, tmp_path: Path) -> None:
        """The grace floor must not re-arm the whole window for a crash loop."""
        rows = [_prompt_row()]

        def _seed(bridge: Path, wire: Path) -> None:
            size = wire.stat().st_size
            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=1,
                    offset=size,
                    turn_open=True,
                    last_activity_ts=time.time() - 4.5,
                    last_seen_offset=size,
                ),
            )

        started = time.monotonic()
        await _drive_loop_until(
            tmp_path, rows, lambda: bool(self.statuses), quiescence_s=5.0, prepare=_seed
        )
        assert self.statuses == [("idle", "")]
        assert time.monotonic() - started <= 2.5

    async def test_same_path_wire_shrink_reseeds_high_water(self, tmp_path: Path) -> None:
        """A wire recreated at the same path below the observed high-water must
        refresh the activity clock and reseed the gate — not leave the stale
        high-water blinding it while quiescence falsely closes the live turn."""
        rows = [_prompt_row()]
        bridge = tmp_path / "bridge"

        def _seed(bridge_dir: Path, wire: Path) -> None:
            # A prior run observed a 100kB wire (delivery stuck at 0) that kimi
            # has since recreated as this much smaller one, 100s ago.
            _write_state(
                bridge_dir,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=0,
                    offset=0,
                    turn_open=True,
                    last_activity_ts=time.time() - 100.0,
                    last_seen_offset=100_000,
                ),
            )

        def _reseeded() -> bool:
            state = _read_state(bridge)
            return (
                state is not None
                and state.last_seen_offset is not None
                and 0 < state.last_seen_offset < 100_000
                and state.offset == state.last_seen_offset
            )

        await _drive_loop_until(tmp_path, rows, _reseeded, quiescence_s=50.0, prepare=_seed)
        assert self.statuses == []

    async def test_replayed_turn_edge_is_deduped(self, tmp_path: Path) -> None:
        """A crash between an edge POST and the cursor persist must not double-post."""
        rows = [_prompt_row(), _turn_ended_row("completed")]
        bridge = tmp_path / "bridge"

        def _seed(bridge_dir: Path, wire: Path) -> None:
            # A prior forwarder posted the edge (line 1) but crashed before
            # advancing the cursor past it.
            _write_state(
                bridge_dir,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=1,
                    offset=len(json.dumps(rows[0])) + 1,
                    turn_open=True,
                    last_edge_id="kimi:turn_end:1",
                ),
            )

        def _consumed() -> bool:
            state = _read_state(bridge)
            return state is not None and state.last_line >= 2 and not state.turn_open

        await _drive_loop_until(tmp_path, rows, _consumed, prepare=_seed)
        assert self.statuses == []


class TestForwardLoopPostFailures:
    async def test_poison_item_dropped_after_bounded_retries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A permanent-4xx item is skipped after bounded retries; the tail moves on."""
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

        rows = [_prompt_row("poison"), _assistant_row("u1", "ok"), _turn_ended_row("completed")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(statuses))
        assert rejections == 3
        assert posted == ["kimi:u1"]
        assert statuses == ["idle"]

    @pytest.mark.parametrize("status_code", [401, 403, 429])
    async def test_auth_and_rate_limit_rejections_retry_and_never_drop(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status_code: int
    ) -> None:
        """401/403/429 are transient outages, not poison: the item must survive."""
        posted: list[str] = []
        rejections = 0

        async def _fake_item(_client: object, **kwargs: object) -> None:
            nonlocal rejections
            if rejections < 4:
                rejections += 1
                request = httpx.Request("POST", "http://test")
                raise httpx.HTTPStatusError(
                    str(status_code),
                    request=request,
                    response=httpx.Response(status_code, request=request),
                )
            posted.append(str(kwargs["item"].response_id))  # type: ignore[union-attr]

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)

        await _drive_loop_until(tmp_path, [_prompt_row("edge")], lambda: bool(posted))
        assert rejections == 4
        assert posted == ["kimi:turn:0"]

    async def test_endless_transient_rejection_degrades_health(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A revoked token's endless 401s must be visible to the idle-turn watchdog."""
        forwarder_health.clear()
        attempts = 0

        async def _fake_item(_client: object, **kwargs: object) -> None:
            nonlocal attempts
            attempts += 1
            request = httpx.Request("POST", "http://test")
            raise httpx.HTTPStatusError(
                "401", request=request, response=httpx.Response(401, request=request)
            )

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)

        await _drive_loop_until(
            tmp_path,
            [_prompt_row()],
            lambda: attempts >= 2 and forwarder_health.recent_post_failure(60.0) is not None,
        )
        forwarder_health.clear()

    async def test_poison_dropped_failed_edge_routes_failed_via_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A permanently rejected turn_failed edge must not be closed as idle later."""
        posted: list[tuple[str, str]] = []
        edge_attempts = 0

        async def _fake_item(_client: object, **kwargs: object) -> None:
            return None

        async def _fake_status(_client: object, **kwargs: object) -> None:
            nonlocal edge_attempts
            edge_attempts += 1
            if edge_attempts <= 3:
                request = httpx.Request("POST", "http://test")
                raise httpx.HTTPStatusError(
                    "404", request=request, response=httpx.Response(404, request=request)
                )
            posted.append((str(kwargs["status"]), str(kwargs["output"])))

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)
        monkeypatch.setattr(fwd, "_post_external_session_status", _fake_status)

        rows = [_prompt_row(), _turn_ended_row("failed", error_message="boom")]
        await _drive_loop_until(tmp_path, rows, lambda: bool(posted), quiescence_s=0.05)
        assert edge_attempts == 4
        assert posted == [("failed", "")]

    async def test_high_water_persisted_without_cursor_advance(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Observed-tail state persists on advancement even while delivery fails,
        so a crash loop can't replay the same rows into a fresh clock."""
        bridge = tmp_path / "bridge"

        async def _failing_item(_client: object, **kwargs: object) -> None:
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(fwd, "_post_conversation_item", _failing_item)

        def _persisted() -> bool:
            state = _read_state(bridge)
            return state is not None and (state.last_seen_offset or 0) > 0 and state.offset == 0

        await _drive_loop_until(tmp_path, [_prompt_row()], _persisted)
        forwarder_health.clear()

    async def test_crash_loop_replay_does_not_rearm_silence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A restart re-reading rows already observed (persisted high-water)
        must resume the silence clock, not restart the full window."""
        statuses: list[tuple[str, str]] = []

        async def _failing_item(_client: object, **kwargs: object) -> None:
            raise httpx.ConnectError("no route to host")

        async def _fake_status(_client: object, **kwargs: object) -> None:
            statuses.append((str(kwargs["status"]), str(kwargs["output"])))

        monkeypatch.setattr(fwd, "_post_conversation_item", _failing_item)
        monkeypatch.setattr(fwd, "_post_external_session_status", _fake_status)

        def _seed(bridge: Path, wire: Path) -> None:
            # A prior crashed run already observed the whole tail 100s ago but
            # never delivered it (cursor still 0).
            _write_state(
                bridge,
                _ForwardState(
                    wire_path=str(wire),
                    last_line=0,
                    offset=0,
                    last_activity_ts=time.time() - 100.0,
                    last_seen_offset=wire.stat().st_size,
                ),
            )

        await _drive_loop_until(
            tmp_path, [_prompt_row()], lambda: bool(statuses), quiescence_s=50.0, prepare=_seed
        )
        assert statuses == [("idle", "")]
        forwarder_health.clear()

    async def test_failed_redelivery_does_not_defer_quiescence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retries re-reading the same unposted tail must not refresh the
        silence timers — under a permanent outage the fallback still fires."""
        statuses: list[tuple[str, str]] = []

        async def _failing_item(_client: object, **kwargs: object) -> None:
            raise httpx.ConnectError("no route to host")

        async def _fake_status(_client: object, **kwargs: object) -> None:
            statuses.append((str(kwargs["status"]), str(kwargs["output"])))

        monkeypatch.setattr(fwd, "_post_conversation_item", _failing_item)
        monkeypatch.setattr(fwd, "_post_external_session_status", _fake_status)

        await _drive_loop_until(
            tmp_path, [_prompt_row()], lambda: bool(statuses), quiescence_s=0.05
        )
        assert statuses == [("idle", "")]
        forwarder_health.clear()

    async def test_persistent_edge_failure_alert_is_rate_limited(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Edge delivery failing continuously past the threshold logs an error."""
        import logging

        monkeypatch.setattr(fwd, "_EDGE_FAILURE_ALERT_S", 0.05)

        async def _fake_item(_client: object, **kwargs: object) -> None:
            return None

        async def _failing_status(_client: object, **kwargs: object) -> None:
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)
        monkeypatch.setattr(fwd, "_post_external_session_status", _failing_status)

        def _alerted() -> bool:
            return any("undelivered" in rec.message for rec in caplog.records)

        with caplog.at_level(logging.ERROR, logger="omnigent.kimi_native_forwarder"):
            await _drive_loop_until(tmp_path, [_prompt_row()], _alerted, pane_alive=lambda: False)
        alerts = [rec for rec in caplog.records if "undelivered" in rec.message]
        assert alerts and alerts[0].levelno == logging.ERROR
        forwarder_health.clear()

    async def test_supersede_over_dropped_failure_is_logged(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """One poll batch: turn A's failed edge poison-drops, prompt B supersedes.
        Session status is single-valued, so B's outcome wins — but the swallowed
        failure must leave a loud trace."""
        import logging

        posted: list[tuple[str, str]] = []
        edge_attempts = 0

        async def _fake_item(_client: object, **kwargs: object) -> None:
            return None

        async def _fake_status(_client: object, **kwargs: object) -> None:
            nonlocal edge_attempts
            edge_attempts += 1
            if edge_attempts <= 3:
                request = httpx.Request("POST", "http://test")
                raise httpx.HTTPStatusError(
                    "404", request=request, response=httpx.Response(404, request=request)
                )
            posted.append((str(kwargs["status"]), str(kwargs["output"])))

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)
        monkeypatch.setattr(fwd, "_post_external_session_status", _fake_status)

        rows = [
            _prompt_row("A"),
            _turn_ended_row("failed", error_message="boom"),
            _prompt_row("B"),
            _assistant_row("u1", "reply"),
            _turn_ended_row("completed"),
        ]
        with caplog.at_level(logging.ERROR, logger="omnigent.kimi_native_forwarder"):
            await _drive_loop_until(tmp_path, rows, lambda: bool(posted))
        assert posted == [("idle", "reply")]
        assert any("superseded" in rec.message for rec in caplog.records)

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

    async def test_fallback_edge_failure_is_attributed_to_health(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A failing pane-death edge POST must show up in forwarder health."""
        forwarder_health.clear()

        async def _fake_item(_client: object, **kwargs: object) -> None:
            return None

        async def _failing_status(_client: object, **kwargs: object) -> None:
            raise httpx.ConnectError("no route to host")

        monkeypatch.setattr(fwd, "_post_conversation_item", _fake_item)
        monkeypatch.setattr(fwd, "_post_external_session_status", _failing_status)

        await _drive_loop_until(
            tmp_path,
            [_prompt_row()],
            lambda: forwarder_health.recent_post_failure(60.0) is not None,
            pane_alive=lambda: False,
        )
        forwarder_health.clear()
