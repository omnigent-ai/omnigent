"""Unit tests for the hermes-native session-store forwarder.

Builds a fixture SQLite store matching Hermes' ``state.db`` schema (``sessions``
with ``cwd`` + ``started_at`` and ``messages`` with a monotonic ``id`` cursor,
plain-text ``content``, and an ``active`` flag) and exercises discovery-by-cwd,
message decode, attachment stripping, role mapping, the claim guard, and the
idempotent high-water cursor.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from omnigent import hermes_native_forwarder as f
from omnigent import hermes_native_status as hstatus

_SCHEMA = """
CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    cwd TEXT,
    started_at REAL NOT NULL,
    parent_session_id TEXT
);
CREATE TABLE messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tool_call_id TEXT,
    tool_calls TEXT,
    tool_name TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    compacted INTEGER NOT NULL DEFAULT 0
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


def test_discover_child_session_returns_newest_child(tmp_path: Path) -> None:
    """After compaction Hermes forks a child via parent_session_id; pick the newest."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.executemany(
        "INSERT INTO sessions(id, source, cwd, started_at, parent_session_id) VALUES (?,?,?,?,?)",
        [
            ("parent", "cli", "/w", 1000.0, None),
            ("child_old", "cli", "/w", 1005.0, "parent"),
            ("child_new", "cli", "/w", 1010.0, "parent"),
            ("unrelated", "cli", "/w", 1011.0, None),
        ],
    )
    con.commit()
    con.close()
    assert f._discover_child_session(db, "parent") == "child_new"
    # No children -> None (forwarder stays pinned to the parent).
    assert f._discover_child_session(db, "child_new") is None


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


async def test_forward_loop_repins_to_child_after_compaction(tmp_path, monkeypatch) -> None:
    """Compaction forks a child session; the forwarder re-pins and mirrors its messages.

    Pre-pins the parent (so discovery is skipped), seeds a compacted parent plus a
    child whose parent_session_id is the parent, then drives a bounded number of
    poll cycles. The first cycle persists the compaction and re-pins to the child;
    the next mirrors the child's messages and persists state under the child id.
    """
    workspace = str(tmp_path)
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.executemany(
        "INSERT INTO sessions(id, source, cwd, started_at, parent_session_id) VALUES (?,?,?,?,?)",
        [
            ("parent_1", "cli", workspace, 1000.0, None),
            ("child_1", "cli", workspace, 1005.0, "parent_1"),
        ],
    )
    con.executemany(
        "INSERT INTO messages(session_id, role, content, active, compacted) VALUES (?,?,?,?,?)",
        [
            ("parent_1", "assistant", "compacted summary", 1, 1),  # parent has compaction
            ("child_1", "user", "child hi", 1, 0),
            ("child_1", "assistant", "child reply", 1, 0),
        ],
    )
    con.commit()
    con.close()

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    # Pre-pin the parent so the loop tails it directly (last_id past the parent's
    # only message so we don't mirror it; compaction triggers the re-pin).
    f._write_state(bridge_dir, f._ForwardState(hermes_session_id="parent_1", last_id=1))

    posted: list[f._MirrorItem] = []

    async def _fake_post(_client, *, session_id, item):
        posted.append(item)

    monkeypatch.setattr(f, "_post_conversation_item", _fake_post)
    monkeypatch.setattr(f, "_persist_hermes_compaction_item", lambda *a, **k: _noop())

    iteration = {"n": 0}

    async def _sleep(_s):
        iteration["n"] += 1
        if iteration["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(f.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await f.forward_hermes_store_to_session(
            base_url="http://x",
            headers={},
            session_id="conv_child",
            bridge_dir=bridge_dir,
            agent_name="hermes-native-ui",
            workspace=workspace,
            launch_epoch_s=1000.0,
            db_path=db,
        )

    # Re-pinned to the child and mirrored its messages.
    roles = [i.item_data.get("role") for i in posted]
    assert roles == ["user", "assistant"]
    assert f._read_state(bridge_dir).hermes_session_id == "child_1"


async def _noop() -> None:
    return None


async def test_forward_loop_rebases_idle_count_on_compaction_repin(tmp_path, monkeypatch) -> None:
    """Compaction re-pin rebases the idle posted-count to the child's count.

    Regression: the completed-turn count is per hermes_session_id, but the idle
    dedup baseline (posted_count) is per bridge dir. A parent that accrued N idle
    posts leaves posted_count=N; the child's count restarts near 0, so without a
    rebase the guard ``completed_turns > posted_count`` stays False until the
    child exceeds N terminal turns — suppressing the child's early idle posts and
    hanging the orchestrator. On re-pin the baseline must drop to the child's
    current completed-turn count so the child's next completion still wakes the
    parent.
    """
    workspace = str(tmp_path)
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.executemany(
        "INSERT INTO sessions(id, source, cwd, started_at, parent_session_id) VALUES (?,?,?,?,?)",
        [
            ("parent_1", "cli", workspace, 1000.0, None),
            ("child_1", "cli", workspace, 1005.0, "parent_1"),
        ],
    )
    con.executemany(
        "INSERT INTO messages(session_id, role, content, tool_calls, active, compacted) "
        "VALUES (?,?,?,?,?,?)",
        [
            # Parent: two completed turns (terminal assistant rows) + a compaction.
            ("parent_1", "assistant", "done 1", None, 1, 0),
            ("parent_1", "assistant", "done 2", None, 1, 0),
            ("parent_1", "assistant", "compacted summary", None, 1, 1),
            # Child: one completed turn so far.
            ("child_1", "user", "child hi", None, 1, 0),
            ("child_1", "assistant", "child done", None, 1, 0),
        ],
    )
    con.commit()
    con.close()

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    # Pin the parent; posted_count reflects the parent's two completed turns.
    f._write_state(bridge_dir, f._ForwardState(hermes_session_id="parent_1", last_id=3))
    hstatus.write_posted_count(bridge_dir, 2)

    async def _fake_post(_client, *, session_id, item):
        return None

    monkeypatch.setattr(f, "_post_conversation_item", _fake_post)
    monkeypatch.setattr(f, "_persist_hermes_compaction_item", lambda *a, **k: _noop())

    idle_posts: list[str] = []

    async def _fake_idle(_client, *, session_id, status):
        idle_posts.append(status)

    monkeypatch.setattr(f, "_post_external_session_status", _fake_idle)

    iteration = {"n": 0}

    async def _sleep(_s):
        iteration["n"] += 1
        if iteration["n"] >= 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(f.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await f.forward_hermes_store_to_session(
            base_url="http://x",
            headers={},
            session_id="conv_child",
            bridge_dir=bridge_dir,
            agent_name="hermes-native-ui",
            workspace=workspace,
            launch_epoch_s=1000.0,
            db_path=db,
        )

    # Re-pinned to the child, and the baseline dropped from the parent's 2 to the
    # child's 1 completed turn — so the child's already-present completion is not
    # suppressed. Without the rebase, posted_count would stay 2 and the guard
    # (1 > 2) would suppress the child's idle.
    assert f._read_state(bridge_dir).hermes_session_id == "child_1"
    assert hstatus.read_posted_count(bridge_dir) == 1
    assert idle_posts == []  # child's single completed turn == baseline, no double-post


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


# --- turn-completion ("idle") parent-wake path --------------------------------
#
# Hermes has no per-turn stop hook (only a ``pre_tool_call`` policy hook), so the
# forwarder derives turn completion from the message log itself: an ``assistant``
# row with no ``tool_calls`` is the agentic loop's terminal step. It POSTs
# ``external_session_status: idle`` once per completed turn — the edge that wakes
# the parent orchestrator — deduped against a persisted posted-count.


def _seed_turns(path: Path, *, cwd: str, started_at: float, session_id: str, n_turns: int) -> None:
    """Seed *n_turns* completed turns: each is user + assistant(final, no tool_calls)."""
    con = sqlite3.connect(path)
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO sessions(id, source, cwd, started_at) VALUES (?,?,?,?)",
        (session_id, "cli", cwd, started_at),
    )
    rows = []
    for i in range(n_turns):
        rows.append((session_id, "user", f"ask {i}", None, None, None, 1))
        rows.append((session_id, "assistant", f"answer {i}", None, None, None, 1))
    con.executemany(
        "INSERT INTO messages"
        "(session_id, role, content, tool_call_id, tool_calls, tool_name, active)"
        " VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()


def test_assistant_row_has_tool_calls() -> None:
    assert f._assistant_row_has_tool_calls(None) is False
    assert f._assistant_row_has_tool_calls("") is False
    assert f._assistant_row_has_tool_calls("[]") is False
    assert f._assistant_row_has_tool_calls("not json") is False
    assert f._assistant_row_has_tool_calls(json.dumps([{"id": "c1"}])) is True


def test_count_completed_turns_counts_no_tool_call_assistant_rows(tmp_path: Path) -> None:
    """A turn ends on an assistant row with no tool_calls; tool-call steps don't count."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO sessions(id, source, cwd, started_at) VALUES (?,?,?,?)",
        ("s1", "cli", str(tmp_path), 1000.0),
    )
    tc = json.dumps([{"id": "c1", "call_id": "c1", "function": {"name": "f", "arguments": "{}"}}])
    rows = [
        ("s1", "user", "go", None, None, None, 1),
        ("s1", "assistant", "", None, tc, None, 1),  # tool-call step -> not terminal
        ("s1", "tool", "result", "c1", None, "f", 1),
        ("s1", "assistant", "final answer", None, None, None, 1),  # terminal -> +1
    ]
    con.executemany(
        "INSERT INTO messages"
        "(session_id, role, content, tool_call_id, tool_calls, tool_name, active)"
        " VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()
    assert f._count_completed_turns(db, "s1") == 1


def test_count_completed_turns_counts_regardless_of_active(tmp_path: Path) -> None:
    """Soft-deleted (compacted, active=0) terminal rows still count, keeping it monotonic."""
    db = tmp_path / "state.db"
    _seed_turns(db, cwd=str(tmp_path), started_at=1000.0, session_id="s1", n_turns=2)
    con = sqlite3.connect(db)
    con.execute("UPDATE messages SET active = 0 WHERE role = 'assistant'")
    con.commit()
    con.close()
    assert f._count_completed_turns(db, "s1") == 2


def test_hermes_status_posted_count_roundtrip_and_clear(tmp_path: Path) -> None:
    bridge = tmp_path / "b"
    assert hstatus.read_posted_count(bridge) == 0
    hstatus.write_posted_count(bridge, 4)
    assert hstatus.read_posted_count(bridge) == 4
    hstatus.clear_hermes_status_state(bridge)
    assert hstatus.read_posted_count(bridge) == 0


async def _run_hermes_loop(
    monkeypatch,
    *,
    db: Path,
    bridge_dir: Path,
    workspace: str,
    statuses: list[str],
    stop_after_iterations: int,
) -> None:
    """Drive the real hermes forward loop with HTTP stubbed; record idle statuses."""

    async def _noop_item(_client, *, session_id, item):
        pass

    async def _record_status(_client, *, session_id, status):
        statuses.append(status)

    monkeypatch.setattr(f, "_post_conversation_item", _noop_item)
    monkeypatch.setattr(f, "_post_external_session_status", _record_status)

    iteration = {"n": 0}

    async def _sleep(_s):
        iteration["n"] += 1
        if iteration["n"] >= stop_after_iterations:
            raise asyncio.CancelledError

    monkeypatch.setattr(f.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await f.forward_hermes_store_to_session(
            base_url="http://x",
            headers={},
            session_id="conv_idle",
            bridge_dir=bridge_dir,
            agent_name="hermes-native-ui",
            workspace=workspace,
            launch_epoch_s=1000.0,
            db_path=db,
        )


async def test_forward_loop_posts_idle_once_per_turn(tmp_path, monkeypatch) -> None:
    workspace = str(tmp_path)
    db = tmp_path / "state.db"
    _seed_turns(db, cwd=workspace, started_at=1000.0, session_id="s1", n_turns=1)
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    statuses: list[str] = []
    await _run_hermes_loop(
        monkeypatch,
        db=db,
        bridge_dir=bridge_dir,
        workspace=workspace,
        statuses=statuses,
        stop_after_iterations=2,  # discover+mirror+idle in iter 1, then stop
    )
    assert statuses == ["idle"]
    assert hstatus.read_posted_count(bridge_dir) == 1


async def test_forward_loop_idle_restart_safe(tmp_path, monkeypatch) -> None:
    """A restart whose posted-count already covers the completed turn posts no idle."""
    workspace = str(tmp_path)
    db = tmp_path / "state.db"
    _seed_turns(db, cwd=workspace, started_at=1000.0, session_id="s1", n_turns=1)
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    hstatus.write_posted_count(bridge_dir, 1)  # already reported before the "restart"
    statuses: list[str] = []
    await _run_hermes_loop(
        monkeypatch,
        db=db,
        bridge_dir=bridge_dir,
        workspace=workspace,
        statuses=statuses,
        stop_after_iterations=3,
    )
    assert statuses == []
    assert hstatus.read_posted_count(bridge_dir) == 1


async def test_forward_loop_idle_dedupes_and_posts_per_new_turn(tmp_path, monkeypatch) -> None:
    """No duplicate idle while quiescent; a turn that lands later posts exactly one more."""
    workspace = str(tmp_path)
    db = tmp_path / "state.db"
    _seed_turns(db, cwd=workspace, started_at=1000.0, session_id="s1", n_turns=1)
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    statuses: list[str] = []

    async def _noop_item(_client, *, session_id, item):
        pass

    async def _record_status(_client, *, session_id, status):
        statuses.append(status)

    monkeypatch.setattr(f, "_post_conversation_item", _noop_item)
    monkeypatch.setattr(f, "_post_external_session_status", _record_status)

    iteration = {"n": 0}

    async def _sleep(_s):
        iteration["n"] += 1
        # After the first turn has been reported, append a second completed turn
        # so the next poll observes a strictly higher count and posts once more.
        if iteration["n"] == 3:
            con = sqlite3.connect(db)
            con.execute(
                "INSERT INTO messages"
                "(session_id, role, content, tool_call_id, tool_calls, tool_name, active)"
                " VALUES (?,?,?,?,?,?,?)",
                ("s1", "assistant", "answer 2", None, None, None, 1),
            )
            con.commit()
            con.close()
        if iteration["n"] >= 6:
            raise asyncio.CancelledError

    monkeypatch.setattr(f.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await f.forward_hermes_store_to_session(
            base_url="http://x",
            headers={},
            session_id="conv_idle",
            bridge_dir=bridge_dir,
            agent_name="hermes-native-ui",
            workspace=workspace,
            launch_epoch_s=1000.0,
            db_path=db,
        )
    # One idle for the seeded turn, one for the turn appended mid-run — never
    # one-per-poll across the six iterations.
    assert statuses == ["idle", "idle"]
    assert hstatus.read_posted_count(bridge_dir) == 2
