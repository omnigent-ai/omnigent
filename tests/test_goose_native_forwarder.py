"""Unit tests for the goose-native session-store forwarder.

Builds a fixture SQLite store matching Goose 1.38.0's verified schema
(``sessions`` + ``messages`` with a monotonic ``id`` cursor and JSON
``content_json``) and exercises discovery-by-name, message decode, attachment
stripping, role mapping, the idempotent high-water cursor, tool-call extraction,
and live-card per-turn response-id grouping. The poll-loop tests at the bottom
drive ``forward_goose_store_to_session`` end to end against a recording poster
to pin the live-card lifecycle (running/idle edges, stalled-turn backstop, and
restart replay).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

from omnigent import goose_native_forwarder as f

_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL DEFAULT '',
    working_dir TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content_json TEXT NOT NULL,
    created_timestamp INTEGER NOT NULL DEFAULT 0
);
"""


def _seed_db(path: Path) -> None:
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO sessions(id, name, working_dir) VALUES('20260619_1', 'omni-1', '/tmp')"
    )
    con.execute(
        "INSERT INTO messages(session_id, role, content_json, created_timestamp) VALUES (?,?,?,?)",
        ("20260619_1", "user", json.dumps([{"type": "text", "text": "hi [Attached: /x.png]"}]), 1),
    )
    con.execute(
        "INSERT INTO messages(session_id, role, content_json, created_timestamp) VALUES (?,?,?,?)",
        ("20260619_1", "assistant", json.dumps([{"type": "text", "text": "hello"}]), 2),
    )
    con.execute(
        "INSERT INTO messages(session_id, role, content_json, created_timestamp) VALUES (?,?,?,?)",
        ("20260619_1", "tool", json.dumps([{"type": "toolresp"}]), 3),
    )
    con.commit()
    con.close()


# ── _content_text ─────────────────────────────────────────────────────────────


def test_content_text_handles_shapes() -> None:
    assert f._content_text(json.dumps("hello")) == "hello"
    assert f._content_text(json.dumps([{"type": "text", "text": "a"}, {"text": "b"}])) == "ab"
    assert f._content_text(json.dumps({"text": "hi"})) == "hi"
    assert f._content_text(json.dumps({"content": "nested"})) == "nested"
    # tool-only / unknown parts → no prose
    assert f._content_text(json.dumps([{"type": "toolreq", "id": "x"}])) == ""
    # non-JSON falls back to the raw string
    assert f._content_text("plain text") == "plain text"


# ── session resolution ────────────────────────────────────────────────────────


def test_resolve_session_id_by_name(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    _seed_db(db)
    assert f._resolve_goose_session_id(db, "omni-1") == "20260619_1"
    assert f._resolve_goose_session_id(db, "missing") is None


# ── _extract_tool_calls ───────────────────────────────────────────────────────


def test_extract_tool_calls_single() -> None:
    content = json.dumps(
        [
            {"type": "text", "text": "Running bash"},
            {"type": "toolreq", "id": "req_1", "name": "bash", "parameters": {"command": "ls"}},
        ]
    )
    calls = f._extract_tool_calls(content)
    assert len(calls) == 1
    tool_id, name, args_json = calls[0]
    assert tool_id == "req_1"
    assert name == "bash"
    assert json.loads(args_json) == {"command": "ls"}


def test_extract_tool_calls_multiple() -> None:
    content = json.dumps(
        [
            {"type": "toolreq", "id": "req_a", "name": "read_file", "parameters": {"path": "/x"}},
            {"type": "toolreq", "id": "req_b", "name": "bash", "parameters": {"command": "pwd"}},
        ]
    )
    calls = f._extract_tool_calls(content)
    assert len(calls) == 2
    assert calls[0][0] == "req_a"
    assert calls[1][0] == "req_b"


def test_extract_tool_calls_tolerates_input_field() -> None:
    """Accepts "input" as a synonym for "parameters"."""
    content = json.dumps(
        [{"type": "toolreq", "id": "req_x", "name": "bash", "input": {"command": "echo hi"}}]
    )
    calls = f._extract_tool_calls(content)
    assert len(calls) == 1
    assert json.loads(calls[0][2]) == {"command": "echo hi"}


def test_extract_tool_calls_empty_on_no_toolreq() -> None:
    content = json.dumps([{"type": "text", "text": "no tools here"}])
    assert f._extract_tool_calls(content) == []


def test_extract_tool_calls_empty_on_invalid_json() -> None:
    assert f._extract_tool_calls("not-json") == []


# ── _extract_tool_result ──────────────────────────────────────────────────────


def test_extract_tool_result_basic() -> None:
    content = json.dumps([{"type": "toolresp", "id": "req_1", "output": "file.txt\nother.txt"}])
    result = f._extract_tool_result(content)
    assert result is not None
    tool_id, output = result
    assert tool_id == "req_1"
    assert "file.txt" in output


def test_extract_tool_result_tolerates_tool_use_id_field() -> None:
    content = json.dumps([{"type": "toolresp", "tool_use_id": "req_2", "output": "done"}])
    result = f._extract_tool_result(content)
    assert result is not None
    assert result[0] == "req_2"


def test_extract_tool_result_none_on_no_toolresp() -> None:
    content = json.dumps([{"type": "toolreq", "id": "x"}])
    assert f._extract_tool_result(content) is None


def test_extract_tool_result_none_on_invalid_json() -> None:
    assert f._extract_tool_result("not-json") is None


# ── _message_to_items ─────────────────────────────────────────────────────────


def test_message_to_items_user() -> None:
    items = f._message_to_items(
        1, "user", json.dumps([{"type": "text", "text": "hi"}]), "goose", None
    )
    assert len(items) == 1
    assert items[0].item_type == "message"
    assert items[0].item_data["role"] == "user"
    assert items[0].response_id == "goose:1"


def test_message_to_items_assistant_prose_only() -> None:
    items = f._message_to_items(
        2, "assistant", json.dumps([{"type": "text", "text": "hello"}]), "goose", "goose:turn:2"
    )
    assert len(items) == 1
    assert items[0].item_type == "message"
    assert items[0].response_id == "goose:turn:2"


def test_message_to_items_assistant_with_tool_call() -> None:
    content = json.dumps(
        [
            {"type": "text", "text": "Running bash"},
            {"type": "toolreq", "id": "req_1", "name": "bash", "parameters": {"command": "ls"}},
        ]
    )
    items = f._message_to_items(3, "assistant", content, "goose", "goose:turn:3")
    # One prose message + one function_call
    assert len(items) == 2
    types = {i.item_type for i in items}
    assert "message" in types
    assert "function_call" in types
    fc = next(i for i in items if i.item_type == "function_call")
    assert fc.item_data["call_id"] == "req_1"
    assert fc.item_data["name"] == "bash"
    assert fc.response_id == "goose:turn:3"


def test_message_to_items_tool_result() -> None:
    content = json.dumps([{"type": "toolresp", "id": "req_1", "output": "ok"}])
    items = f._message_to_items(4, "tool", content, "goose", "goose:turn:3")
    assert len(items) == 1
    assert items[0].item_type == "function_call_output"
    assert items[0].item_data["call_id"] == "req_1"
    assert items[0].item_data["output"] == "ok"
    assert items[0].response_id == "goose:turn:3"


def test_message_to_items_strips_attachment_markers() -> None:
    items = f._message_to_items(
        1,
        "user",
        json.dumps([{"type": "text", "text": "hi [Attached: /x.png]"}]),
        "goose",
        None,
    )
    assert items[0].item_data["content"][0]["text"] == "hi"


def test_message_to_items_empty_assistant_skipped() -> None:
    """A tool-only assistant row with no prose produces only function_call items."""
    content = json.dumps([{"type": "toolreq", "id": "req_x", "name": "bash", "parameters": {}}])
    items = f._message_to_items(5, "assistant", content, "goose", "goose:turn:5")
    assert all(i.item_type == "function_call" for i in items)


def test_message_to_items_system_role_skipped() -> None:
    items = f._message_to_items(
        6, "system", json.dumps([{"type": "text", "text": "sys"}]), "goose", None
    )
    assert items == []


# ── _read_new_items (backward compat) ─────────────────────────────────────────


def test_read_new_items_maps_roles_and_strips_attachments(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    _seed_db(db)
    items = f._read_new_items(db, "20260619_1", 0, "goose-native-ui")
    posted = [i for i in items if i.item_type]
    assert len(posted) == 2
    assert posted[0].item_data == {
        "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],  # attachment marker stripped
    }
    assert posted[1].item_data["role"] == "assistant"
    assert posted[1].item_data["agent"] == "goose-native-ui"
    assert posted[1].item_data["content"] == [{"type": "output_text", "text": "hello"}]


def test_cursor_is_idempotent_past_high_water(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    _seed_db(db)
    items = f._read_new_items(db, "20260619_1", 0, "goose-native-ui")
    max_id = max(i.msg_id for i in items)
    # The tool row (id=3) is the last; re-reading past it yields nothing.
    assert f._read_new_items(db, "20260619_1", max_id, "goose-native-ui") == []


# ── state persistence ─────────────────────────────────────────────────────────


def test_state_roundtrip_and_clear(tmp_path: Path) -> None:
    state = f._ForwardState(goose_session_id="20260619_1", last_id=7)
    assert f._write_state(tmp_path, state) is True
    loaded = f._read_state(tmp_path)
    assert loaded.goose_session_id == "20260619_1" and loaded.last_id == 7
    f.clear_goose_bridge_state(tmp_path)
    assert f._read_state(tmp_path) == f._ForwardState()


def test_default_sessions_db_honors_override(monkeypatch) -> None:
    monkeypatch.setenv("GOOSE_SESSIONS_DB", "/custom/sessions.db")
    assert f.default_sessions_db() == Path("/custom/sessions.db")
    monkeypatch.delenv("GOOSE_SESSIONS_DB", raising=False)
    assert f.default_sessions_db().name == "sessions.db"


# ── poll-loop live-card lifecycle ─────────────────────────────────────────────


_SID = "20260619_1"


def _seed_empty_db(path: Path) -> None:
    """Schema + session row only; tests insert messages as the 'turn' unfolds."""
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    con.execute(f"INSERT INTO sessions(id, name, working_dir) VALUES('{_SID}', 'omni-1', '/tmp')")
    con.commit()
    con.close()


def _insert(db: Path, role: str, content: list[dict]) -> int:
    con = sqlite3.connect(db)
    cur = con.execute(
        "INSERT INTO messages(session_id, role, content_json, created_timestamp) VALUES (?,?,?,0)",
        (_SID, role, json.dumps(content)),
    )
    con.commit()
    rowid = cur.lastrowid
    con.close()
    assert rowid is not None
    return rowid


def _prose(text: str) -> list[dict]:
    return [{"type": "text", "text": text}]


def _toolreq(call_id: str) -> list[dict]:
    return [{"type": "toolreq", "id": call_id, "name": "bash", "parameters": {"cmd": "ls"}}]


def _toolresp(call_id: str) -> list[dict]:
    return [{"type": "toolresp", "id": call_id, "output": "ok"}]


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


async def _run_loop(db, bridge_dir, rec, monkeypatch, until, max_ticks=1500) -> None:
    """Run the real poll loop against *db* + *rec* until *until()* holds.

    Raises if the condition is never reached within *max_ticks* (~2ms each) —
    i.e. the loop wedged or the expected posts never happened.
    """
    monkeypatch.setattr(f, "post_external_session_status", rec.post_status)
    monkeypatch.setattr(f, "_post_conversation_item", rec.post_item)
    task = asyncio.create_task(
        f.forward_goose_store_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_1",
            bridge_dir=bridge_dir,
            agent_name="goose-native",
            goose_session_name="omni-1",
            db_path=db,
            poll_interval_s=0.001,
        )
    )
    try:
        for _ in range(max_ticks):
            if until():
                break
            await asyncio.sleep(0.002)
        else:
            raise AssertionError(f"loop never reached expected state; events={rec.events}")
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def _idle_seen(rec: _Recorder, turn: str):
    """Until-condition: the closing idle edge for *turn* has been posted."""

    def check() -> bool:
        return ("status", "idle", turn) in rec.events

    return check


async def test_turn_closes_immediately_on_final_prose(tmp_path, monkeypatch) -> None:
    # The normal close: Goose's loop ends on an assistant reply with no tool
    # calls, so the idle edge must land right after that row is mirrored — not
    # after a quiescence wait (the stall backstop is minutes away).
    db = tmp_path / "sessions.db"
    _seed_empty_db(db)
    _insert(db, "user", _prose("hi"))
    req_id = _insert(db, "assistant", _toolreq("call_1"))
    _insert(db, "tool", _toolresp("call_1"))
    _insert(db, "assistant", _prose("done"))
    turn = f"goose:turn:{req_id}"

    rec = _Recorder()
    await _run_loop(db, tmp_path / "bridge", rec, monkeypatch, _idle_seen(rec, turn))

    assert rec.statuses() == [("running", turn), ("idle", turn)]
    # running precedes the turn's first item; idle follows its last.
    assert rec.events.index(("status", "running", turn)) < rec.events.index(
        ("item", "function_call", turn)
    )
    assert rec.events.index(("status", "idle", turn)) > rec.events.index(("item", "message", turn))


async def test_no_idle_while_tool_call_runs_long(tmp_path, monkeypatch) -> None:
    # A tool call writes no store rows while it executes; quiet alone must not
    # close the card (the old 8s quiescence flickered on every long call).
    db = tmp_path / "sessions.db"
    _seed_empty_db(db)
    _insert(db, "user", _prose("hi"))
    req_id = _insert(db, "assistant", _toolreq("call_1"))
    turn = f"goose:turn:{req_id}"

    rec = _Recorder()
    db_done: list[bool] = []

    def _finished() -> bool:
        if not db_done and ("item", "function_call", turn) in rec.events:
            # Card is live; let the "tool" run quietly for a while, then land
            # its result + the final prose.
            db_done.append(True)
            loop = asyncio.get_running_loop()
            loop.call_later(0.3, _insert, db, "tool", _toolresp("call_1"))
            loop.call_later(0.3, _insert, db, "assistant", _prose("done"))
        return ("status", "idle", turn) in rec.events

    await _run_loop(db, tmp_path / "bridge", rec, monkeypatch, _finished)

    # Exactly one running and one closing idle — no mid-call flicker.
    assert rec.statuses() == [("running", turn), ("idle", turn)]


async def test_stalled_turn_backstop_closes_and_resume_rejoins(tmp_path, monkeypatch) -> None:
    # A turn that dies without its normal close (interrupt/crash) is closed by
    # the backstop; if rows do arrive later they rejoin the same turn id.
    monkeypatch.setattr(f, "_STALLED_TURN_IDLE_S", 0.15)
    db = tmp_path / "sessions.db"
    _seed_empty_db(db)
    _insert(db, "user", _prose("hi"))
    req_id = _insert(db, "assistant", _toolreq("call_1"))
    turn = f"goose:turn:{req_id}"

    rec = _Recorder()
    resumed: list[bool] = []
    full = [("running", turn), ("idle", turn), ("running", turn), ("idle", turn)]

    def staged() -> bool:
        if not resumed and rec.statuses() == full[:2]:
            # Backstop closed the quiet turn; the result + final prose arriving
            # late must re-open and re-close the SAME turn, not mint a new one.
            resumed.append(True)
            _insert(db, "tool", _toolresp("call_1"))
            _insert(db, "assistant", _prose("done"))
        return rec.statuses() == full

    await _run_loop(db, tmp_path / "bridge", rec, monkeypatch, staged)
    assert rec.statuses() == full


async def test_restart_replay_resumes_turn_id_without_reposting(tmp_path, monkeypatch) -> None:
    # Crash simulation: the previous run mirrored the toolreq row (cursor=req
    # row, running edge already posted) and died mid-tool-call. The restart must
    # adopt the original turn id for the remaining rows — not re-post running,
    # not mint a fresh id that would split the streaming group.
    db = tmp_path / "sessions.db"
    _seed_empty_db(db)
    _insert(db, "user", _prose("hi"))
    req_id = _insert(db, "assistant", _toolreq("call_1"))
    _insert(db, "tool", _toolresp("call_1"))
    _insert(db, "assistant", _prose("done"))
    turn = f"goose:turn:{req_id}"

    bridge = tmp_path / "bridge"
    f._write_state(bridge, f._ForwardState(goose_session_id=_SID, last_id=req_id))

    rec = _Recorder()
    await _run_loop(db, bridge, rec, monkeypatch, _idle_seen(rec, turn))

    assert rec.statuses() == [("idle", turn)]  # no duplicate running
    assert ("item", "function_call_output", turn) in rec.events
    assert ("item", "message", turn) in rec.events


async def test_restart_replay_closes_already_finished_turn(tmp_path, monkeypatch) -> None:
    # Crash simulation: the previous run mirrored the turn's final prose row but
    # died before posting the closing idle. The restart must close the card (a
    # redundant idle is harmless; a spinner that never settles is not).
    db = tmp_path / "sessions.db"
    _seed_empty_db(db)
    _insert(db, "user", _prose("hi"))
    prose_id = _insert(db, "assistant", _prose("all done"))
    turn = f"goose:turn:{prose_id}"

    bridge = tmp_path / "bridge"
    f._write_state(bridge, f._ForwardState(goose_session_id=_SID, last_id=prose_id))

    rec = _Recorder()
    await _run_loop(db, bridge, rec, monkeypatch, _idle_seen(rec, turn))
    assert rec.events[0] == ("status", "idle", turn)

    # And the forwarder is clean for the next turn: a fresh user message and
    # reply run the normal lifecycle under a new turn id. (A restart replays
    # the same close first — the cursor never moved past the finished turn.)
    _insert(db, "user", _prose("thanks"))
    next_id = _insert(db, "assistant", _prose("np"))
    next_turn = f"goose:turn:{next_id}"
    rec2 = _Recorder()
    await _run_loop(db, bridge, rec2, monkeypatch, _idle_seen(rec2, next_turn))
    assert rec2.statuses() == [("idle", turn), ("running", next_turn), ("idle", next_turn)]
