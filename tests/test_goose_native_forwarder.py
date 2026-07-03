"""Unit tests for the goose-native session-store forwarder.

Builds a fixture SQLite store matching Goose 1.38.0's verified schema
(``sessions`` + ``messages`` with a monotonic ``id`` cursor and JSON
``content_json``) and exercises discovery-by-name, message decode, attachment
stripping, role mapping, and the idempotent high-water cursor.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

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


async def _run_forward_until(tmp_path: Path, done, extra_s: float = 0.1) -> None:
    task = asyncio.create_task(
        f.forward_goose_store_to_session(
            base_url="http://test",
            headers={},
            session_id="conv",
            bridge_dir=tmp_path / "bridge",
            agent_name="goose-native-ui",
            goose_session_name="omni-1",
            db_path=tmp_path / "sessions.db",
            poll_interval_s=0.01,
        )
    )
    try:
        for _ in range(600):
            await asyncio.sleep(0.01)
            if done():
                break
        # A few more polls so a wrongful re-post would show up.
        await asyncio.sleep(extra_s)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_ambiguous_post_failure_is_not_reposted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An item whose POST response was lost is skipped, not re-posted.

    The server does not dedupe ``external_conversation_item`` POSTs, so a
    blind retry after an ambiguous failure (request sent, response lost)
    duplicates the web bubble. Mirrors cursor-native's handling.
    """
    _seed_db(tmp_path / "sessions.db")
    delivered: list[int] = []

    async def _fake_post(_client: object, *, session_id: str, item: object) -> None:
        delivered.append(item.msg_id)  # type: ignore[attr-defined]
        if len(delivered) == 1:
            # The server committed the item but the response was lost.
            raise httpx.ReadTimeout("response lost after delivery")

    monkeypatch.setattr(f, "_post_conversation_item", _fake_post)
    await _run_forward_until(tmp_path, lambda: len(set(delivered)) >= 2)

    # The first row was delivered exactly once despite the lost response.
    assert delivered == sorted(set(delivered))


async def test_poison_item_is_skipped_after_bounded_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deterministically rejected item stops wedging the mirror forever."""
    _seed_db(tmp_path / "sessions.db")
    delivered: list[int] = []
    poison_id: list[int] = []

    async def _fake_post(_client: object, *, session_id: str, item: object) -> None:
        delivered.append(item.msg_id)  # type: ignore[attr-defined]
        if not poison_id:
            poison_id.append(item.msg_id)  # type: ignore[attr-defined]
        if item.msg_id == poison_id[0]:  # type: ignore[attr-defined]
            request = httpx.Request("POST", "http://ap")
            raise httpx.HTTPStatusError(
                "rejected", request=request, response=httpx.Response(400, request=request)
            )

    monkeypatch.setattr(f, "_post_conversation_item", _fake_post)
    await _run_forward_until(tmp_path, lambda: len(set(delivered)) >= 2, extra_s=0.05)

    # Bounded retries for the poison row, then the mirror moves on.
    assert delivered.count(poison_id[0]) == f._MAX_ITEM_POST_ATTEMPTS
    later = [m for m in delivered if m != poison_id[0]]
    assert later and len(later) == len(set(later))
