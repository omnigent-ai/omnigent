"""Unit tests for the coco-native hook-event forwarder.

Builds fixture ``hook_events.jsonl`` + ``<session_id>.history.jsonl`` files
matching CoCo v1.1.1's verified shapes (Anthropic-style ``text`` / ``tool_use``
/ ``tool_result`` content blocks) and exercises the durable cursor round trip,
partial-line-safe event tailing, history-path derivation, block-to-item
mapping, and 1-based history line numbering. The poll-loop tests at the bottom
drive ``forward_coco_events_to_session`` end to end against a recording poster
to pin session adoption (resume seeds the cursor, no re-mirror), the per-turn
``coco:turn:<n>`` running/idle lifecycle, the stalled-turn backstop, and the
supervisor's crash-restart backoff.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from omnigent import coco_native_forwarder as f
from omnigent.coco_native_hook import HOOK_EVENTS_FILE

# ── state persistence ─────────────────────────────────────────────────────────


def test_state_roundtrip(tmp_path: Path) -> None:
    state = f._ForwardState(
        coco_session_id="coco-1",
        history_path="/conv/coco-1.history.jsonl",
        last_line=5,
        events_offset=123,
        turn_seq=2,
    )
    assert f._write_state(tmp_path, state) is True
    loaded = f._read_state(tmp_path)
    assert loaded == state


def test_read_state_missing_or_corrupt_is_cold_default(tmp_path: Path) -> None:
    assert f._read_state(tmp_path) == f._ForwardState()
    (tmp_path / "coco_forwarder.json").write_text("not-json", encoding="utf-8")
    assert f._read_state(tmp_path) == f._ForwardState()
    # A JSON scalar (not an object) is also cold.
    (tmp_path / "coco_forwarder.json").write_text('"hi"', encoding="utf-8")
    assert f._read_state(tmp_path) == f._ForwardState()
    # Wrong-typed / negative fields fall back per-field.
    (tmp_path / "coco_forwarder.json").write_text(
        json.dumps({"coco_session_id": 7, "last_line": -1, "turn_seq": 3}), encoding="utf-8"
    )
    loaded = f._read_state(tmp_path)
    assert loaded.coco_session_id is None
    assert loaded.last_line == 0
    assert loaded.turn_seq == 3


def test_clear_coco_bridge_state_removes_state_and_event_log(tmp_path: Path) -> None:
    f._write_state(tmp_path, f._ForwardState(coco_session_id="coco-1"))
    (tmp_path / HOOK_EVENTS_FILE).write_text('{"hook_event_name": "Stop"}\n', encoding="utf-8")
    f.clear_coco_bridge_state(tmp_path)
    assert not (tmp_path / "coco_forwarder.json").exists()
    assert not (tmp_path / HOOK_EVENTS_FILE).exists()
    assert f._read_state(tmp_path) == f._ForwardState()
    # Clearing an already-clean dir is a no-op, not an error.
    f.clear_coco_bridge_state(tmp_path)


# ── _read_new_events ──────────────────────────────────────────────────────────


def test_read_new_events_missing_file(tmp_path: Path) -> None:
    events, offset = f._read_new_events(tmp_path / HOOK_EVENTS_FILE, 3)
    assert events == []
    assert offset == 3


def test_read_new_events_leaves_trailing_partial_unconsumed(tmp_path: Path) -> None:
    path = tmp_path / HOOK_EVENTS_FILE
    whole = b'{"hook_event_name": "SessionStart"}\n'
    path.write_bytes(whole + b'{"hook_event_name": "St')  # hook mid-write
    events, offset = f._read_new_events(path, 0)
    assert [e["hook_event_name"] for e in events] == ["SessionStart"]
    assert offset == len(whole)  # the partial line stays for the next poll
    # The write completes; the next poll picks it up whole from the offset.
    with open(path, "ab") as fh:
        fh.write(b'op"}\n')
    events, offset = f._read_new_events(path, offset)
    assert [e["hook_event_name"] for e in events] == ["Stop"]
    assert offset == path.stat().st_size


def test_read_new_events_skips_malformed_interior_lines(tmp_path: Path) -> None:
    path = tmp_path / HOOK_EVENTS_FILE
    path.write_bytes(b'{"a": 1}\nnot-json\n[1, 2]\n{"b": 2}\n')
    events, offset = f._read_new_events(path, 0)
    # Bad JSON and non-dict lines are dropped but still consumed.
    assert events == [{"a": 1}, {"b": 2}]
    assert offset == path.stat().st_size
    # Fully consumed: a re-read from the new offset yields nothing.
    assert f._read_new_events(path, offset) == ([], offset)


# ── _history_path_for_transcript ──────────────────────────────────────────────


def test_history_path_for_transcript_shapes() -> None:
    assert f._history_path_for_transcript("/conv/S1.json") == "/conv/S1.history.jsonl"
    # Already the history file -> passthrough.
    assert f._history_path_for_transcript("/conv/S1.history.jsonl") == "/conv/S1.history.jsonl"
    assert f._history_path_for_transcript("") is None
    assert f._history_path_for_transcript("/conv/S1.txt") is None


# ── _message_to_items ─────────────────────────────────────────────────────────


def test_message_to_items_user_text_strips_attachment_marker() -> None:
    obj = {"role": "user", "content": [{"type": "text", "text": "hi [Attached: /x.png]"}]}
    items = f._message_to_items(3, obj, "coco", "coco:turn:1")
    assert len(items) == 1
    assert items[0].item_type == "message"
    assert items[0].item_data == {
        "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],
    }
    # User bubbles keep the per-line id even while a turn is open.
    assert items[0].response_id == "coco:3"


def test_message_to_items_assistant_text() -> None:
    obj = {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}
    items = f._message_to_items(4, obj, "coco-native-ui", "coco:turn:2")
    assert len(items) == 1
    assert items[0].item_type == "message"
    assert items[0].item_data["agent"] == "coco-native-ui"
    assert items[0].item_data["content"] == [{"type": "output_text", "text": "hello"}]
    assert items[0].response_id == "coco:turn:2"


def test_message_to_items_assistant_falls_back_to_per_line_id() -> None:
    obj = {"role": "assistant", "content": [{"type": "text", "text": "hello"}]}
    items = f._message_to_items(7, obj, "coco", None)
    assert items[0].response_id == "coco:7"


def test_message_to_items_tool_use() -> None:
    obj = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "Running ls"},
            {
                "type": "tool_use",
                "tool_use": {"tool_use_id": "tu_1", "name": "bash", "input": {"command": "ls"}},
            },
        ],
    }
    items = f._message_to_items(5, obj, "coco", "coco:turn:1")
    assert [i.item_type for i in items] == ["function_call", "message"]
    fc = items[0]
    assert fc.item_data["call_id"] == "tu_1"
    assert fc.item_data["name"] == "bash"
    assert json.loads(fc.item_data["arguments"]) == {"command": "ls"}
    assert fc.response_id == "coco:turn:1"


def test_message_to_items_tool_use_requires_call_id_and_assistant_role() -> None:
    block = {"type": "tool_use", "tool_use": {"name": "bash", "input": {}}}  # no tool_use_id
    assert f._message_to_items(1, {"role": "assistant", "content": [block]}, "coco", None) == []
    # A user-row tool_use (never valid in CoCo history) is ignored.
    block = {"type": "tool_use", "tool_use": {"tool_use_id": "tu_x", "name": "bash"}}
    assert f._message_to_items(1, {"role": "user", "content": [block]}, "coco", None) == []


def test_message_to_items_tool_result_content_shapes() -> None:
    def result_row(content: Any) -> dict[str, Any]:
        block = {"type": "tool_result", "tool_result": {"tool_use_id": "tu_1", "content": content}}
        return {"role": "user", "content": [block]}

    # Plain-string content.
    items = f._message_to_items(6, result_row("file.txt"), "coco", "coco:turn:1")
    assert len(items) == 1
    assert items[0].item_type == "function_call_output"
    assert items[0].item_data == {"call_id": "tu_1", "output": "file.txt"}
    assert items[0].response_id == "coco:turn:1"
    # List content flattens strings and text parts; non-text parts are dropped.
    mixed = ["a", {"type": "text", "text": "b"}, {"type": "image", "source": {}}, 42]
    items = f._message_to_items(6, result_row(mixed), "coco", None)
    assert items[0].item_data["output"] == "ab"
    # Unrecognized content -> empty output, item still posted for the call.
    items = f._message_to_items(6, result_row(None), "coco", None)
    assert items[0].item_data["output"] == ""


def test_message_to_items_skips_image_nondict_and_unknown_roles() -> None:
    obj = {
        "role": "assistant",
        "content": [{"type": "image", "source": {"data": "..."}}, "stray-string", 42],
    }
    assert f._message_to_items(1, obj, "coco", None) == []
    assert (
        f._message_to_items(1, {"role": "system", "content": [{"type": "text"}]}, "coco", None)
        == []
    )
    assert f._message_to_items(1, {"role": "user", "content": "not-a-list"}, "coco", None) == []


# ── _read_history_lines ───────────────────────────────────────────────────────


def _user_row(text: str) -> str:
    return json.dumps({"role": "user", "content": [{"type": "text", "text": text}]})


def _assistant_row(text: str) -> str:
    return json.dumps({"role": "assistant", "content": [{"type": "text", "text": text}]})


def test_read_history_lines_numbers_past_cursor(tmp_path: Path) -> None:
    path = tmp_path / "S1.history.jsonl"
    path.write_text(_user_row("one") + "\n" + _assistant_row("two") + "\n", encoding="utf-8")
    rows = f._read_history_lines(str(path), 0)
    assert [n for n, _ in rows] == [1, 2]
    assert rows[1][1]["role"] == "assistant"
    # Resuming past line 1 yields only line 2.
    rows = f._read_history_lines(str(path), 1)
    assert [n for n, _ in rows] == [2]
    assert f._read_history_lines(str(path), 2) == []
    assert f._read_history_lines(str(tmp_path / "missing.jsonl"), 0) == []


def test_read_history_lines_trailing_partial_excluded_interior_bad_advances(
    tmp_path: Path,
) -> None:
    path = tmp_path / "S1.history.jsonl"
    path.write_text(
        _user_row("one") + "\n" + "not-json\n" + _assistant_row("three") + "\n" + '{"partial',
        encoding="utf-8",
    )
    rows = f._read_history_lines(str(path), 0)
    # The interior bad line yields an empty row (so the cursor still advances
    # past it); the unterminated tail is mid-write and excluded entirely.
    assert rows == [
        (1, json.loads(_user_row("one"))),
        (2, {}),
        (3, json.loads(_assistant_row("three"))),
    ]


# ── poll-loop lifecycle ───────────────────────────────────────────────────────

_SID = "coco-sess-1"


class _Recorder:
    """Records every status edge and mirrored item the loop posts, in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, str | None]] = []

    async def post_status(self, client, *, session_id, status, response_id=None, **_) -> None:
        self.events.append(("status", status, response_id))

    async def post_item(self, client, *, session_id, item) -> None:
        self.events.append(("item", item.item_type, item.response_id))

    def statuses(self) -> list[tuple[str, str | None]]:
        return [(status, rid) for kind, status, rid in self.events if kind == "status"]


class _Fixture:
    """A bridge dir + conversations dir wired like the coco-native bridge."""

    def __init__(self, tmp_path: Path) -> None:
        self.bridge_dir = tmp_path / "bridge"
        self.bridge_dir.mkdir()
        conversations = tmp_path / "conversations"
        conversations.mkdir()
        self.transcript_path = conversations / f"{_SID}.json"
        self.history_path = conversations / f"{_SID}.history.jsonl"

    def emit(self, kind: str) -> None:
        """Append one hook event as the CoCo lifecycle hook would."""
        with open(self.bridge_dir / HOOK_EVENTS_FILE, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps(
                    {
                        "hook_event_name": kind,
                        "session_id": _SID,
                        "transcript_path": str(self.transcript_path),
                    }
                )
                + "\n"
            )

    def append_history(self, *rows: str) -> None:
        with open(self.history_path, "a", encoding="utf-8") as fh:
            for row in rows:
                fh.write(row + "\n")


async def _run_loop(fx: _Fixture, rec: _Recorder, monkeypatch, until, *, on_sid=None) -> None:
    """Run the real poll loop against *fx* + *rec* until *until()* holds.

    Raises if the condition is never reached within ~3s — i.e. the loop wedged
    or the expected posts never happened.
    """
    monkeypatch.setattr(f, "post_external_session_status", rec.post_status)
    monkeypatch.setattr(f, "_post_conversation_item", rec.post_item)
    task = asyncio.create_task(
        f.forward_coco_events_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_1",
            bridge_dir=fx.bridge_dir,
            agent_name="coco-native",
            on_coco_session_id=on_sid,
            poll_interval_s=0.001,
        )
    )
    try:
        for _ in range(1500):
            if until():
                break
            await asyncio.sleep(0.002)
        else:
            raise AssertionError(f"loop never reached expected state; events={rec.events}")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_forward_loop_adopts_mirrors_turns_and_resumes(tmp_path, monkeypatch) -> None:
    """End to end: adoption seeds the cursor past pre-existing history (resume
    must not re-mirror what Omnigent already holds), each UserPromptSubmit/Stop
    pair runs one ``coco:turn:<n>`` running/idle lifecycle, and the session-id
    callback fires exactly once for the one discovered session."""
    fx = _Fixture(tmp_path)
    # A resumed session opens with its prior turns already flushed.
    fx.append_history(_user_row("old ask"), _assistant_row("old answer"))
    fx.emit("SessionStart")

    seen_sids: list[str] = []

    async def _on_sid(sid: str) -> None:
        seen_sids.append(sid)

    rec = _Recorder()
    monkeypatch.setattr(f, "post_external_session_status", rec.post_status)
    monkeypatch.setattr(f, "_post_conversation_item", rec.post_item)
    task = asyncio.create_task(
        f.forward_coco_events_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_1",
            bridge_dir=fx.bridge_dir,
            agent_name="coco-native",
            on_coco_session_id=_on_sid,
            poll_interval_s=0.001,
        )
    )

    async def _wait(until) -> None:
        for _ in range(1500):
            if until():
                return
            await asyncio.sleep(0.002)
        raise AssertionError(f"loop never reached expected state; events={rec.events}")

    try:
        # Adoption: session id persisted, cursor seeded past the 2 old lines.
        await _wait(lambda: f._read_state(fx.bridge_dir).coco_session_id == _SID)
        assert f._read_state(fx.bridge_dir).last_line == 2
        assert seen_sids == [_SID]
        assert rec.events == []  # pre-existing history was NOT re-mirrored

        # Turn 1: prompt submitted -> user bubble mirrored, running edge opens.
        fx.append_history(_user_row("hi [Attached: /x.png]"))
        fx.emit("UserPromptSubmit")
        await _wait(lambda: ("status", "running", "coco:turn:1") in rec.events)
        # The user bubble is standalone (per-line id, line 3), mirrored before
        # the turn opened.
        assert rec.events.index(("item", "message", "coco:3")) < rec.events.index(
            ("status", "running", "coco:turn:1")
        )

        # Turn 1 finishes: CoCo flushes the reply, then fires Stop.
        fx.append_history(
            json.dumps(
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "on it"},
                        {
                            "type": "tool_use",
                            "tool_use": {"tool_use_id": "tu_1", "name": "bash", "input": {}},
                        },
                    ],
                }
            ),
            json.dumps(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_result": {"tool_use_id": "tu_1", "content": "ok"},
                        }
                    ],
                }
            ),
            _assistant_row("done"),
        )
        fx.emit("Stop")
        await _wait(lambda: ("status", "idle", "coco:turn:1") in rec.events)
        # The whole reply shares the turn id and precedes the closing idle.
        for item_type in ("function_call", "function_call_output", "message"):
            assert ("item", item_type, "coco:turn:1") in rec.events
        assert rec.events[-1] == ("status", "idle", "coco:turn:1")

        # Turn 2 mints the next id; the callback does not re-fire.
        fx.append_history(_user_row("again"))
        fx.emit("UserPromptSubmit")
        await _wait(lambda: ("status", "running", "coco:turn:2") in rec.events)
        fx.append_history(_assistant_row("done again"))
        fx.emit("Stop")
        await _wait(lambda: ("status", "idle", "coco:turn:2") in rec.events)
        assert rec.statuses() == [
            ("running", "coco:turn:1"),
            ("idle", "coco:turn:1"),
            ("running", "coco:turn:2"),
            ("idle", "coco:turn:2"),
        ]
        assert seen_sids == [_SID]
        assert f._read_state(fx.bridge_dir).turn_seq == 2
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_stalled_turn_backstop_closes_without_stop(tmp_path, monkeypatch) -> None:
    """A turn whose Stop hook never fires (killed pane, CoCo crash) is closed by
    the inactivity backstop instead of spinning forever."""
    monkeypatch.setattr(f, "_STALLED_TURN_IDLE_S", 0.05)
    fx = _Fixture(tmp_path)
    fx.history_path.touch()
    fx.emit("SessionStart")
    fx.emit("UserPromptSubmit")

    rec = _Recorder()
    await _run_loop(fx, rec, monkeypatch, lambda: ("status", "idle", "coco:turn:1") in rec.events)
    assert rec.statuses() == [("running", "coco:turn:1"), ("idle", "coco:turn:1")]


# ── supervisor ────────────────────────────────────────────────────────────────


def _supervisor_kwargs(tmp_path: Path) -> dict[str, Any]:
    return {
        "base_url": "http://test",
        "headers": {},
        "session_id": "conv_1",
        "bridge_dir": tmp_path / "bridge",
        "agent_name": "coco-native",
    }


async def test_supervisor_restarts_with_backoff_and_propagates_cancel(
    tmp_path, monkeypatch
) -> None:
    crashes = {"n": 0}

    async def fake_forwarder(**_: Any) -> None:
        crashes["n"] += 1
        if crashes["n"] <= 3:
            raise RuntimeError(f"simulated crash {crashes['n']}")
        raise asyncio.CancelledError()

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    # Pin monotonic so every run looks instantaneous and the healthy-uptime
    # reset branch never fires.
    monkeypatch.setattr(f, "_supervisor_monotonic", lambda: 1000.0)
    monkeypatch.setattr(f, "forward_coco_events_to_session", fake_forwarder)
    monkeypatch.setattr(f, "_supervisor_sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await f.supervise_coco_forwarder(**_supervisor_kwargs(tmp_path))

    # 3 crashes -> 3 doubling backoff sleeps; the CancelledError on run 4
    # propagates without a further sleep.
    assert sleeps == [1.0, 2.0, 4.0]
    assert crashes["n"] == 4


async def test_supervisor_resets_backoff_after_healthy_uptime(tmp_path, monkeypatch) -> None:
    healthy = f._SUPERVISOR_HEALTHY_UPTIME_S
    # Two readings per iteration (run start, run end): runs 1-2 are instant
    # crashes, run 3 exceeds the healthy uptime so backoff resets before its
    # post-run sleep.
    monotonic_values = iter([0.0, 1.0, 10.0, 11.0, 20.0, 20.0 + healthy + 1.0, 200.0, 201.0])
    crashes = {"n": 0}

    async def fake_forwarder(**_: Any) -> None:
        crashes["n"] += 1
        if crashes["n"] >= 4:
            raise asyncio.CancelledError()
        raise RuntimeError("simulated crash")

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(f, "_supervisor_monotonic", lambda: next(monotonic_values))
    monkeypatch.setattr(f, "forward_coco_events_to_session", fake_forwarder)
    monkeypatch.setattr(f, "_supervisor_sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await f.supervise_coco_forwarder(**_supervisor_kwargs(tmp_path))

    # Run 3's healthy uptime resets the backoff to the initial value; without
    # the reset the third sleep would be 4.0.
    assert sleeps == [1.0, 2.0, f._SUPERVISOR_INITIAL_BACKOFF_S]


def test_message_to_items_strips_coco_system_reminder_blocks() -> None:
    """CoCo's injected ``<system-reminder>`` context blocks never reach the web bubble."""
    from omnigent.coco_native_forwarder import _message_to_items

    row = {
        "role": "user",
        "content": [
            {"type": "text", "text": "<system-reminder>\n## Rules\nstuff\n</system-reminder>\n"},
            {"type": "text", "text": "<system-reminder>more plumbing</system-reminder>\n"},
            {"type": "text", "text": "Reply with exactly: e2e-ok."},
        ],
    }
    items = _message_to_items(3, row, "coco-native-ui", None)
    assert len(items) == 1
    (item,) = items
    assert item.item_data["content"] == [
        {"type": "input_text", "text": "Reply with exactly: e2e-ok."}
    ]


def test_message_to_items_reminder_only_user_row_yields_nothing() -> None:
    """A user row that is pure injected context (no real text) mirrors nothing."""
    from omnigent.coco_native_forwarder import _message_to_items

    row = {
        "role": "user",
        "content": [{"type": "text", "text": "<system-reminder>only plumbing</system-reminder>"}],
    }
    assert _message_to_items(4, row, "coco-native-ui", None) == []


def test_state_round_trips_turn_live(tmp_path) -> None:
    """``turn_live`` survives a state write/read cycle (restart restore)."""
    from omnigent.coco_native_forwarder import _ForwardState, _read_state, _write_state

    state = _ForwardState(coco_session_id="sid", history_path="/x.history.jsonl", turn_seq=3)
    state.turn_live = True
    assert _write_state(tmp_path, state)
    restored = _read_state(tmp_path)
    assert restored.turn_live is True and restored.turn_seq == 3


def test_mirror_cursor_snaps_down_when_history_shrinks(tmp_path, monkeypatch) -> None:
    """A compacted (shorter) history file resets the cursor instead of going dead."""
    fx = _Fixture(tmp_path)
    fx.append_history(_user_row("hi"), _assistant_row("yo"))
    # Persisted cursor claims five lines were already mirrored (pre-compaction).
    f._write_state(
        fx.bridge_dir,
        f._ForwardState(coco_session_id=_SID, history_path=str(fx.history_path), last_line=5),
    )
    fx.emit("Stop")
    rec = _Recorder()

    async def _drive() -> None:
        await _run_loop(fx, rec, monkeypatch, lambda: f._read_state(fx.bridge_dir).last_line == 2)

    asyncio.run(_drive())
    # Cursor snapped 5 -> 2 (the rewritten span counts as seen); nothing was
    # re-mirrored, and the next appended row would mirror normally.
    assert rec.events == []


def test_restart_with_live_turn_closes_it_on_stop(tmp_path, monkeypatch) -> None:
    """A restart mid-turn restores the open turn; Stop closes the ORIGINAL id."""
    fx = _Fixture(tmp_path)
    fx.append_history(_assistant_row("late reply"))
    # Persisted state says turn 7 opened (running posted) before the crash.
    f._write_state(
        fx.bridge_dir,
        f._ForwardState(
            coco_session_id=_SID,
            history_path=str(fx.history_path),
            last_line=0,
            turn_seq=7,
            turn_live=True,
        ),
    )
    fx.emit("Stop")
    rec = _Recorder()

    async def _drive() -> None:
        await _run_loop(
            fx, rec, monkeypatch, lambda: ("status", "idle", "coco:turn:7") in rec.events
        )

    asyncio.run(_drive())
    # The restored turn id stamps the mirrored rows AND the closing idle edge;
    # no fresh turn id was minted for the recovered turn.
    assert ("item", "message", "coco:turn:7") in rec.events
    assert f._read_state(fx.bridge_dir).turn_live is False
