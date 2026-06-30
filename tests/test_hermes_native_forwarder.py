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
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
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
    # (session_id, role, content, tool_call_id, tool_calls, tool_name, active)
    rows = [
        (session_id, "user", "hi [Attached: /x.png]", None, None, None, 1),
        (session_id, "assistant", "hello", None, None, None, 1),
        (session_id, "tool", "{tool-result}", None, None, None, 1),  # no tool_call_id -> skipped
        (session_id, "assistant", "", None, None, None, 1),  # no prose, no tool_calls -> skipped
        (session_id, "user", "soft-deleted", None, None, None, 0),  # inactive -> skipped
    ]
    con.executemany(
        "INSERT INTO messages"
        "(session_id, role, content, tool_call_id, tool_calls, tool_name, active)"
        " VALUES (?,?,?,?,?,?,?)",
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


def test_read_new_items_mirrors_tool_calls(tmp_path: Path) -> None:
    """Tool calls on assistant rows become function_call items; tool rows become outputs."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO sessions(id, source, cwd, started_at) VALUES (?,?,?,?)",
        ("s1", "cli", str(tmp_path), 1000.0),
    )
    import json

    tool_calls_json = json.dumps(
        [
            {
                "id": "call_abc",
                "call_id": "call_abc",
                "type": "function",
                "function": {"name": "search_files", "arguments": '{"pattern": "*"}'},
            }
        ]
    )
    rows = [
        ("s1", "assistant", "", None, tool_calls_json, None, 1),
        ("s1", "tool", "found 3 files", "call_abc", None, "search_files", 1),
    ]
    con.executemany(
        "INSERT INTO messages"
        "(session_id, role, content, tool_call_id, tool_calls, tool_name, active)"
        " VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()

    items = f._read_new_items(db, "s1", 0, "agent")
    posted = [i for i in items if i.item_type]
    assert len(posted) == 2
    assert posted[0].item_type == "function_call"
    assert posted[0].item_data["name"] == "search_files"
    assert posted[0].item_data["call_id"] == "call_abc"
    assert posted[1].item_type == "function_call_output"
    assert posted[1].item_data["call_id"] == "call_abc"
    assert posted[1].item_data["output"] == "found 3 files"


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
        self.patches: list[tuple[str, dict]] = []

    async def post(self, url, json=None, **_kwargs):
        self.posts.append((url, json or {}))
        return _Resp()

    async def patch(self, url, json=None, **_kwargs):
        self.patches.append((url, json or {}))
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


async def test_forward_loop_patches_external_session_id_once(tmp_path, monkeypatch) -> None:
    """The forwarder PATCHes external_session_id when it first discovers the Hermes session.

    Runs the full forward loop with all HTTP calls intercepted at the
    ``httpx.AsyncClient`` level (constructor replaced by a fake async-context-
    manager). The first ``test_forward_loop_discovers_and_mirrors_new_messages``
    test creates a *real* ``httpx.AsyncClient`` which can interfere with
    class-level patches on subsequent tests, so we replace the constructor
    entirely to stay fully in-process.
    """
    workspace = str(tmp_path)
    db = tmp_path / "state.db"
    _seed_db(db, cwd=workspace, started_at=1000.0)

    patched_calls: list[tuple[str, dict]] = []

    async def _fake_post(_client, *, session_id, item):
        pass  # ignore mirrored items for this test

    monkeypatch.setattr(f, "_post_conversation_item", _fake_post)

    iteration = {"n": 0}

    # Build a self-contained fake client + constructor so the forward loop
    # never touches real httpx internals.
    class _Client:
        async def post(self, url, json=None, **_kw):
            return _Resp()

        async def patch(self, url, json=None, **_kw):
            patched_calls.append((url, json or {}))
            return _Resp()

    import contextlib

    @contextlib.asynccontextmanager
    async def _make_client(**_kw):
        yield _Client()

    # Patch the module attribute that ``forward_hermes_store_to_session`` reads
    # at call time (``httpx.AsyncClient``).  Using ``monkeypatch.setattr`` on
    # the *module* object the forwarder imports (``f.httpx``) guarantees the
    # right target and automatic undo.
    monkeypatch.setattr(
        f,
        "httpx",
        type(
            "_httpx",
            (),
            {
                "AsyncClient": _make_client,
                "Timeout": lambda *a, **kw: None,
                "Auth": None,
                "HTTPError": Exception,
            },
        ),
    )

    async def _sleep(_s):
        iteration["n"] += 1
        if iteration["n"] >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(asyncio, "sleep", _sleep)

    # Use a subdirectory for bridge_dir so the claim guard doesn't see
    # sibling test directories (which may contain state from earlier tests
    # that used the same hermes session id).
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    with pytest.raises(asyncio.CancelledError):
        await f.forward_hermes_store_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_patch",
            bridge_dir=bridge_dir,
            agent_name="hermes-native-ui",
            workspace=workspace,
            launch_epoch_s=1000.0,
            db_path=db,
        )

    # The PATCH should have been called exactly once even though we ran 3 iterations.
    patch_calls = [(url, body) for url, body in patched_calls if "external_session_id" in body]
    assert len(patch_calls) == 1
    url, body = patch_calls[0]
    assert url == "/v1/sessions/conv_patch"
    assert body["external_session_id"] == "20260620_1"


# --- Usage tracker tests ---------------------------------------------------


async def test_usage_tracker_posts_model_on_first_flush(tmp_path, monkeypatch) -> None:
    """The tracker reads the model from the bridge config and posts it."""
    # Write a per-session config with a model.
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    import yaml

    (hermes_home / "config.yaml").write_text(yaml.dump({"model": "claude-sonnet-4-20250514"}))

    client = _FakeClient()
    tracker = f._HermesUsageTracker(client, "conv_usage", tmp_path)
    await tracker.flush()

    assert len(client.posts) == 1
    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_usage/events"
    assert body["type"] == "external_session_usage"
    assert body["data"]["model"] == "claude-sonnet-4-20250514"


async def test_usage_tracker_deduplicates(tmp_path, monkeypatch) -> None:
    """Consecutive flushes with the same model do not re-post."""
    hermes_home = tmp_path / "hermes_home"
    hermes_home.mkdir()
    import yaml

    (hermes_home / "config.yaml").write_text(yaml.dump({"model": "gpt-4o"}))

    client = _FakeClient()
    tracker = f._HermesUsageTracker(client, "conv_dedup", tmp_path)
    await tracker.flush()
    await tracker.flush()
    await tracker.flush()

    assert len(client.posts) == 1  # only the first flush posts


async def test_usage_tracker_no_post_when_no_model(tmp_path) -> None:
    """No config / no model -> nothing posted."""
    client = _FakeClient()
    tracker = f._HermesUsageTracker(client, "conv_none", tmp_path)
    await tracker.flush()
    assert len(client.posts) == 0


async def test_read_model_from_hermes_config_fallback(tmp_path, monkeypatch) -> None:
    """Falls back to ~/.hermes/config.yaml when no per-session config exists."""
    user_hermes = tmp_path / ".hermes"
    user_hermes.mkdir()
    import yaml

    (user_hermes / "config.yaml").write_text(yaml.dump({"model": "from-user-config"}))
    monkeypatch.setattr(f.Path, "home", staticmethod(lambda: tmp_path))

    model = f._read_model_from_hermes_config(tmp_path / "nonexistent")
    assert model == "from-user-config"


# --- Compaction persistence tests -------------------------------------------

_COMPACTION_SCHEMA = """
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0,
    timestamp REAL,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT
);
"""


def _make_compaction_db(path: Path) -> None:
    """Create a messages-only DB with the compacted column."""
    con = sqlite3.connect(path)
    con.executescript(_COMPACTION_SCHEMA)
    con.commit()
    con.close()


def test_has_new_compaction_returns_true_when_compacted_rows_exist(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_compaction_db(db)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO messages(session_id, role, content, active, compacted)"
        " VALUES (?, ?, ?, 1, 1)",
        (
            "hermes_sess",
            "assistant",
            "compacted summary",
        ),
    )
    con.commit()
    con.close()
    assert f._has_new_compaction(db, "hermes_sess") is True


def test_has_new_compaction_returns_false_when_no_compacted_rows(tmp_path: Path) -> None:
    db = tmp_path / "state.db"
    _make_compaction_db(db)
    con = sqlite3.connect(db)
    con.execute(
        "INSERT INTO messages(session_id, role, content, active, compacted)"
        " VALUES (?, ?, ?, 1, 0)",
        ("hermes_sess", "user", "hello"),
    )
    con.commit()
    con.close()
    assert f._has_new_compaction(db, "hermes_sess") is False


async def test_persist_hermes_compaction_item_posts_with_messages(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock, MagicMock

    db = tmp_path / "state.db"
    _make_compaction_db(db)
    con = sqlite3.connect(db)
    con.executemany(
        "INSERT INTO messages(session_id, role, content, active, compacted)"
        " VALUES (?, ?, ?, ?, ?)",
        [
            ("hermes_sess", "user", "please help", 1, 0),
            ("hermes_sess", "assistant", "sure thing", 1, 0),
        ],
    )
    con.commit()
    con.close()

    get_resp = MagicMock()
    get_resp.raise_for_status = MagicMock()
    get_resp.json = MagicMock(return_value={"data": [{"id": "item_hermes"}]})

    post_resp = MagicMock()
    post_resp.raise_for_status = MagicMock()

    client = AsyncMock()
    client.get = AsyncMock(return_value=get_resp)
    client.post = AsyncMock(return_value=post_resp)

    await f._persist_hermes_compaction_item(
        client,
        session_id="conv_hermes",
        db_path=db,
        hermes_session_id="hermes_sess",
    )

    client.post.assert_called_once()
    _url, kwargs = client.post.call_args
    body = kwargs.get("json") or client.post.call_args[1]["json"]
    assert body["type"] == "compaction"
    assert body["data"]["last_item_id"] == "item_hermes"
    assert len(body["data"]["compacted_messages"]) == 2
    assert body["data"]["compacted_messages"][0]["role"] == "user"
    assert body["data"]["compacted_messages"][1]["role"] == "assistant"


async def test_persist_hermes_compaction_item_empty_db(tmp_path: Path) -> None:
    from unittest.mock import AsyncMock, MagicMock

    db = tmp_path / "state.db"
    _make_compaction_db(db)

    get_resp = MagicMock()
    get_resp.raise_for_status = MagicMock()
    get_resp.json = MagicMock(return_value={"data": []})

    post_resp = MagicMock()
    post_resp.raise_for_status = MagicMock()

    client = AsyncMock()
    client.get = AsyncMock(return_value=get_resp)
    client.post = AsyncMock(return_value=post_resp)

    await f._persist_hermes_compaction_item(
        client,
        session_id="conv_hermes",
        db_path=db,
        hermes_session_id="hermes_sess",
    )

    client.post.assert_called_once()
    _url, kwargs = client.post.call_args
    body = kwargs.get("json") or client.post.call_args[1]["json"]
    assert body["type"] == "compaction"
    assert body["data"]["last_item_id"].startswith("compact_boundary_")
    assert "compacted_messages" not in body["data"]
