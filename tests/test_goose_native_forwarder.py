"""Unit tests for the goose-native session-store forwarder.

Builds a fixture SQLite store matching Goose 1.38.0's verified schema
(``sessions`` + ``messages`` with a monotonic ``id`` cursor and JSON
``content_json``) and exercises discovery-by-name, message decode, attachment
stripping, role mapping, and the idempotent high-water cursor.
"""

from __future__ import annotations

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


def test_content_text_handles_shapes() -> None:
    assert f._content_text(json.dumps("hello")) == "hello"
    assert f._content_text(json.dumps([{"type": "text", "text": "a"}, {"text": "b"}])) == "ab"
    assert f._content_text(json.dumps({"text": "hi"})) == "hi"
    assert f._content_text(json.dumps({"content": "nested"})) == "nested"
    # tool-only / unknown parts → no prose
    assert f._content_text(json.dumps([{"type": "toolreq", "id": "x"}])) == ""
    # non-JSON falls back to the raw string
    assert f._content_text("plain text") == "plain text"


def test_resolve_session_id_by_name(tmp_path: Path) -> None:
    db = tmp_path / "sessions.db"
    _seed_db(db)
    assert f._resolve_goose_session_id(db, "omni-1") == "20260619_1"
    assert f._resolve_goose_session_id(db, "missing") is None


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
