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


@pytest.mark.asyncio
async def test_post_conversation_item_uses_session_event_retry_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []

    async def sink(**kwargs: object) -> httpx.Response:
        calls.append(kwargs)
        return httpx.Response(200, request=httpx.Request("POST", "http://t/"))

    monkeypatch.setattr(f, "post_session_event_with_retry", sink, raising=False)
    item = f._MirrorItem(
        msg_id=1,
        item_type="message",
        item_data={"role": "assistant"},
        response_id="goose:1",
    )

    response = await f._post_conversation_item(object(), session_id="conv goose", item=item)  # type: ignore[arg-type]

    assert response.status_code == 200
    assert len(calls) == 1
    assert calls[0]["event_type"] == "external_conversation_item"
    assert calls[0]["url"] == "/v1/sessions/conv goose/events"
    payload = calls[0]["payload"]
    assert isinstance(payload, dict)
    assert payload["type"] == "external_conversation_item"


@pytest.mark.asyncio
async def test_post_conversation_item_records_connectivity_failure_for_watchdog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent import _native_forwarder_health as health

    class _AlwaysConnectError:
        async def post(self, url: str, *, json: object) -> httpx.Response:
            del json
            raise httpx.ConnectError("No route to host", request=httpx.Request("POST", url))

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(f, "_sleep", no_sleep, raising=False)
    item = f._MirrorItem(
        msg_id=1,
        item_type="message",
        item_data={"role": "assistant"},
        response_id="goose:1",
    )

    health.clear()
    try:
        response = await f._post_conversation_item(
            _AlwaysConnectError(),  # type: ignore[arg-type]
            session_id="conv_x",
            item=item,
        )
        assert response is None
        detail = health.recent_post_failure(60.0)
        assert detail is not None
        assert "external_conversation_item" in detail
        assert "No route to host" in detail
    finally:
        health.clear()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_response", [503, None])
async def test_forwarder_does_not_advance_cursor_past_failed_item(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failed_response: int | None,
) -> None:
    db = tmp_path / "sessions.db"
    _seed_db(db)
    bridge_dir = tmp_path / "bridge"
    calls = 0

    async def post_stub(
        client: httpx.AsyncClient, *, session_id: str, item: f._MirrorItem
    ) -> httpx.Response | None:
        nonlocal calls
        del client, session_id
        calls += 1
        if calls == 1:
            return httpx.Response(200, request=httpx.Request("POST", "http://t/"))
        assert item.msg_id == 2
        if failed_response is None:
            return None
        return httpx.Response(failed_response, request=httpx.Request("POST", "http://t/"))

    async def stop_after_poll(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(f, "_post_conversation_item", post_stub)
    monkeypatch.setattr(f.asyncio, "sleep", stop_after_poll)

    with pytest.raises(asyncio.CancelledError):
        await f.forward_goose_store_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_x",
            bridge_dir=bridge_dir,
            agent_name="goose-native-ui",
            goose_session_name="omni-1",
            db_path=db,
            poll_interval_s=0,
        )

    state = f._read_state(bridge_dir)
    assert calls == 2
    assert state.goose_session_id == "20260619_1"
    assert state.last_id == 1


@pytest.mark.asyncio
async def test_forwarder_advances_cursor_after_successful_batch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    db = tmp_path / "sessions.db"
    _seed_db(db)
    bridge_dir = tmp_path / "bridge"
    posted_ids: list[int] = []

    async def post_stub(
        client: httpx.AsyncClient, *, session_id: str, item: f._MirrorItem
    ) -> httpx.Response:
        del client, session_id
        posted_ids.append(item.msg_id)
        return httpx.Response(200, request=httpx.Request("POST", "http://t/"))

    async def stop_after_poll(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(f, "_post_conversation_item", post_stub)
    monkeypatch.setattr(f.asyncio, "sleep", stop_after_poll)

    with pytest.raises(asyncio.CancelledError):
        await f.forward_goose_store_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_x",
            bridge_dir=bridge_dir,
            agent_name="goose-native-ui",
            goose_session_name="omni-1",
            db_path=db,
            poll_interval_s=0,
        )

    state = f._read_state(bridge_dir)
    assert posted_ids == [1, 2]
    assert state.goose_session_id == "20260619_1"
    assert state.last_id == 3
