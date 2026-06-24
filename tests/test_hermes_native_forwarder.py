"""Unit tests for the hermes-native session-store forwarder.

Builds a fixture SQLite store matching Hermes' ``state.db`` schema (``sessions``
with ``cwd`` + ``started_at`` and ``messages`` with a monotonic ``id`` cursor,
plain-text ``content``, and an ``active`` flag) and exercises discovery-by-cwd,
message decode, attachment stripping, role mapping, the claim guard, and the
idempotent high-water cursor.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from omnigent import hermes_native_forwarder as f

_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    cwd TEXT,
    started_at REAL NOT NULL
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    active INTEGER NOT NULL DEFAULT 1
);
"""


def _seed_db(path: Path, *, cwd: str, started_at: float, session_id: str = "20260620_1") -> None:
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO sessions(id, source, cwd, started_at) VALUES (?,?,?,?)",
        (session_id, "cli", cwd, started_at),
    )
    rows = [
        (session_id, "user", "hi [Attached: /x.png]", 1),
        (session_id, "assistant", "hello", 1),
        (session_id, "tool", "{tool-result}", 1),
        (session_id, "assistant", "", 1),  # reasoning/tool-only: no prose -> skipped
        (session_id, "user", "soft-deleted", 0),  # inactive -> skipped
    ]
    con.executemany(
        "INSERT INTO messages(session_id, role, content, active) VALUES (?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()


def test_discover_session_id_by_cwd_and_floor(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    db = tmp_path / "state.db"
    _seed_db(db, cwd=workspace, started_at=1000.0)
    # Launch floor before the session's started_at -> discovered.
    assert f._discover_session_id(db, workspace, 1000.0) == "20260620_1"
    # A floor far in the future (beyond skew) excludes it.
    assert f._discover_session_id(db, workspace, 2000.0) is None
    # A different workspace with no other candidates -> no match.
    assert f._discover_session_id(db, "/some/other/dir", 1000.0) is None


def test_discover_lone_candidate_only_when_no_cwd_recorded(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    # Hermes recorded no cwd (NULL) — bind the lone candidate past the floor.
    con.execute(
        "INSERT INTO sessions(id, source, cwd, started_at) VALUES (?,?,?,?)",
        ("S_nocwd", "cli", None, 1000.0),
    )
    con.commit()
    con.close()
    assert f._discover_session_id(db, "/whatever", 1000.0) == "S_nocwd"


def test_discover_skips_excluded_session(tmp_path: Path) -> None:
    workspace = str(tmp_path)
    db = tmp_path / "state.db"
    _seed_db(db, cwd=workspace, started_at=1000.0)
    assert (
        f._discover_session_id(db, workspace, 1000.0, excluded=frozenset({"20260620_1"})) is None
    )


def test_read_new_items_maps_roles_and_strips_attachments(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed_db(db, cwd=str(tmp_path), started_at=1000.0)
    items = f._read_new_items(db, "20260620_1", 0, "hermes-native-ui")
    posted = [i for i in items if i.item_type]
    assert len(posted) == 2  # user + assistant("hello"); tool/empty/inactive skipped
    assert posted[0].item_data == {
        "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],  # attachment marker stripped
    }
    assert posted[1].item_data["role"] == "assistant"
    assert posted[1].item_data["agent"] == "hermes-native-ui"
    assert posted[1].item_data["content"] == [{"type": "output_text", "text": "hello"}]


def test_read_new_items_idempotent_past_high_water(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _seed_db(db, cwd=str(tmp_path), started_at=1000.0)
    items = f._read_new_items(db, "20260620_1", 0, "hermes-native-ui")
    max_id = max(i.msg_id for i in items)
    assert f._read_new_items(db, "20260620_1", max_id, "hermes-native-ui") == []


def test_session_claimed_by_other_earlier_launch_wins(tmp_path: Path) -> None:
    root = tmp_path / "hermes-native"
    mine = root / "me"
    other = root / "other"
    mine.mkdir(parents=True)
    other.mkdir(parents=True)
    # A live sibling claims the same session id with an EARLIER launch -> it wins.
    f._write_state(other, f._ForwardState(hermes_session_id="S1", last_id=0, launch_epoch_s=100.0))
    assert f._session_claimed_by_other(mine, "S1", my_launch_s=200.0) is True
    # A different session id is not a conflict.
    assert f._session_claimed_by_other(mine, "S2", my_launch_s=200.0) is False
    # If I launched earlier, I keep the row (sibling does not win).
    assert f._session_claimed_by_other(mine, "S1", my_launch_s=50.0) is False


def test_state_roundtrip_and_clear(tmp_path: Path) -> None:
    state = f._ForwardState(hermes_session_id="20260620_1", last_id=7, launch_epoch_s=12.5)
    assert f._write_state(tmp_path, state) is True
    loaded = f._read_state(tmp_path)
    assert loaded.hermes_session_id == "20260620_1"
    assert loaded.last_id == 7
    assert loaded.launch_epoch_s == 12.5
    f.clear_hermes_bridge_state(tmp_path)
    assert f._read_state(tmp_path) == f._ForwardState()


def test_default_state_db_honors_overrides(monkeypatch) -> None:
    monkeypatch.setenv("HERMES_STATE_DB", "/custom/state.db")
    assert f.default_state_db() == Path("/custom/state.db")
    monkeypatch.delenv("HERMES_STATE_DB", raising=False)
    monkeypatch.setenv("HERMES_HOME", "/opt/hermes-home")
    assert f.default_state_db() == Path("/opt/hermes-home/state.db")
    monkeypatch.delenv("HERMES_HOME", raising=False)
    assert f.default_state_db().name == "state.db"


# --- forwarder loop + POST plumbing -------------------------------------------


class _Resp:
    def __init__(self, status: int = 200) -> None:
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"status {self.status_code}")


class _FakeClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url, json=None, **_kwargs):
        self.posts.append((url, json or {}))
        return _Resp()


async def test_post_conversation_item_posts_event(tmp_path) -> None:
    client = _FakeClient()
    item = f._MirrorItem(
        msg_id=5,
        item_type="message",
        item_data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        response_id="hermes:5",
    )
    await f._post_conversation_item(client, session_id="conv_q", item=item)
    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_q/events"
    assert body["type"] == "external_conversation_item"
    assert body["data"]["response_id"] == "hermes:5"


async def test_forward_loop_discovers_and_mirrors_new_messages(tmp_path, monkeypatch) -> None:
    """One forward iteration: discover the session by cwd+floor, mirror user+assistant."""
    workspace = str(tmp_path)
    db = tmp_path / "state.db"
    _seed_db(db, cwd=workspace, started_at=1000.0)

    posted: list[f._MirrorItem] = []

    async def _fake_post(_client, *, session_id, item):
        posted.append(item)

    monkeypatch.setattr(f, "_post_conversation_item", _fake_post)

    calls = {"n": 0}

    async def _sleep(_s):
        calls["n"] += 1
        raise asyncio.CancelledError  # stop after the first full iteration

    monkeypatch.setattr(f.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await f.forward_hermes_store_to_session(
            base_url="http://x",
            headers={},
            session_id="conv_f",
            bridge_dir=tmp_path,
            agent_name="hermes-native-ui",
            workspace=workspace,
            launch_epoch_s=1000.0,
            db_path=db,
        )
    # The seeded user + assistant("hello") rows mirrored (tool/empty/inactive skipped).
    roles = [i.item_data.get("role") for i in posted]
    assert roles == ["user", "assistant"]
    # High-water cursor persisted so a restart resumes without re-posting.
    assert f._read_state(tmp_path).hermes_session_id == "20260620_1"
