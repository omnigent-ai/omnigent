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
    reasoning_content TEXT,
    reasoning TEXT,
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
    # (session_id, role, content, tool_call_id, tool_calls, tool_name, reasoning_content,
    #  reasoning, active)
    rows = [
        (session_id, "user", "hi [Attached: /x.png]", None, None, None, None, None, 1),
        (session_id, "assistant", "hello", None, None, None, None, None, 1),
        (session_id, "tool", "{tool-result}", None, None, None, None, None, 1),  # no id -> skip
        (session_id, "assistant", "", None, None, None, None, None, 1),  # no prose/tools -> skip
        (session_id, "user", "soft-deleted", None, None, None, None, None, 0),  # inactive -> skip
    ]
    con.executemany(
        "INSERT INTO messages"
        "(session_id, role, content, tool_call_id, tool_calls, tool_name, reasoning_content,"
        " reasoning, active)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
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


def test_read_new_items_mirrors_reasoning_before_message(tmp_path: Path) -> None:
    """An assistant row with reasoning posts a one-shot reasoning delta before the message."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO sessions(id, source, cwd, started_at) VALUES (?,?,?,?)",
        ("s1", "cli", str(tmp_path), 1000.0),
    )
    con.execute(
        "INSERT INTO messages"
        "(session_id, role, content, reasoning_content, reasoning, active)"
        " VALUES (?,?,?,?,?,?)",
        ("s1", "assistant", "done", "thinking hard [Attached: /x]", "fallback", 1),
    )
    con.commit()
    con.close()
    items = f._read_new_items(db, "s1", 0, "hermes-native-ui")
    posted = [i for i in items if i.item_type]
    assert posted[0].item_type == "external_output_reasoning_delta"
    assert posted[0].item_data == {"delta": "thinking hard", "started": True}  # marker stripped
    assert posted[1].item_type == "message"
    assert posted[1].item_data["content"] == [{"type": "output_text", "text": "done"}]


def test_read_new_items_no_reasoning_when_columns_empty(tmp_path: Path) -> None:
    """An assistant row without reasoning posts no reasoning delta (the seeded "hello" row)."""
    db = tmp_path / "state.db"
    _seed_db(db, cwd=str(tmp_path), started_at=1000.0)
    items = f._read_new_items(db, "20260620_1", 0, "hermes-native-ui")
    assert not any(i.item_type == "external_output_reasoning_delta" for i in items)


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


def test_assistant_prose_precedes_its_tool_calls(tmp_path: Path) -> None:
    """An assistant row with BOTH prose and tool_calls emits the message first,
    then the function_call items. The prose is the model's preamble ('I'll run
    X…'), so it belongs before the calls; it also keeps the in-flight tool as the
    trailing item on the web so its live spinner renders (the tool card would go
    static if a trailing message followed it)."""
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO sessions(id, source, cwd, started_at) VALUES (?,?,?,?)",
        ("s1", "cli", str(tmp_path), 1000.0),
    )
    tc = json.dumps(
        [
            {"id": "c1", "call_id": "c1", "function": {"name": "terminal", "arguments": "{}"}},
            {"id": "c2", "call_id": "c2", "function": {"name": "terminal", "arguments": "{}"}},
        ]
    )
    con.execute(
        "INSERT INTO messages"
        "(session_id, role, content, tool_call_id, tool_calls, tool_name, active)"
        " VALUES (?,?,?,?,?,?,?)",
        ("s1", "assistant", "I'll run the sleep command twice in parallel.", None, tc, None, 1),
    )
    con.commit()
    con.close()

    posted = [i for i in f._read_new_items(db, "s1", 0, "agent") if i.item_type]
    assert [i.item_type for i in posted] == ["message", "function_call", "function_call"]
    assert posted[0].item_data["role"] == "assistant"


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


async def test_post_conversation_item_posts_reasoning_delta(tmp_path) -> None:
    client = _FakeClient()
    item = f._MirrorItem(
        msg_id=6,
        item_type="external_output_reasoning_delta",
        item_data={"delta": "let me think", "started": True},
        response_id="hermes:6",
    )
    await f._post_conversation_item(client, session_id="conv_q", item=item)
    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_q/events"
    assert body["type"] == "external_output_reasoning_delta"
    assert body["data"] == {"delta": "let me think", "started": True}


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

    async def _fake_idle(_client, *, session_id, status, response_id=None):
        # This test isolates the idle/parent-wake dedup; the running edge (live
        # card) is covered by the _annotate_turn_actions tests below.
        if status == "idle":
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


def test_count_completed_turns_respects_max_id(tmp_path: Path) -> None:
    """Terminal rows above the mirrored high-water mark are not counted yet."""
    db = tmp_path / "state.db"
    # Rows: 1=user, 2=assistant(final), 3=user, 4=assistant(final).
    _seed_turns(db, cwd=str(tmp_path), started_at=1000.0, session_id="s1", n_turns=2)
    assert f._count_completed_turns(db, "s1", max_id=1) == 0
    assert f._count_completed_turns(db, "s1", max_id=2) == 1
    assert f._count_completed_turns(db, "s1", max_id=3) == 1
    assert f._count_completed_turns(db, "s1", max_id=4) == 2
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

    async def _record_status(_client, *, session_id, status, response_id=None):
        # Idle-only: these loop tests assert the parent-wake idle dedup; the
        # running edge has dedicated coverage in the _annotate_turn_actions tests.
        if status == "idle":
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

    async def _record_status(_client, *, session_id, status, response_id=None):
        if status == "idle":
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


async def test_forward_loop_idle_waits_for_mid_delivery_final_message(
    tmp_path, monkeypatch
) -> None:
    """A final assistant row landing while a poll's batch is still being
    delivered must not ring idle until that row is itself mirrored — the idle
    edge wakes the parent orchestrator, which then reads the transcript, so the
    completion signal may never overtake the content it announces."""
    workspace = str(tmp_path)
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO sessions(id, source, cwd, started_at) VALUES (?,?,?,?)",
        ("s1", "cli", workspace, 1000.0),
    )
    tc = json.dumps([{"id": "c1", "call_id": "c1", "function": {"name": "f", "arguments": "{}"}}])
    con.executemany(
        "INSERT INTO messages"
        "(session_id, role, content, tool_call_id, tool_calls, tool_name, active)"
        " VALUES (?,?,?,?,?,?,?)",
        [
            ("s1", "user", "go", None, None, None, 1),
            ("s1", "assistant", "", None, tc, None, 1),  # mid-turn tool-call step
            ("s1", "tool", "result", "c1", None, "f", 1),
        ],
    )
    con.commit()
    con.close()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    # One ordered log for both channels so the content/signal order is provable.
    events: list[tuple[str, object]] = []
    landed = {"done": False}

    async def _post_item(_client, *, session_id, item):
        events.append(("item", item.msg_id))
        if not landed["done"]:
            # The turn's final assistant row lands while this batch is still
            # being POSTed — after the mirror's read, before the idle check.
            landed["done"] = True
            con = sqlite3.connect(db)
            con.execute(
                "INSERT INTO messages"
                "(session_id, role, content, tool_call_id, tool_calls, tool_name, active)"
                " VALUES (?,?,?,?,?,?,?)",
                ("s1", "assistant", "final answer", None, None, None, 1),
            )
            con.commit()
            con.close()

    async def _record_status(_client, *, session_id, status, response_id=None):
        events.append(("status", status))

    monkeypatch.setattr(f, "_post_conversation_item", _post_item)
    monkeypatch.setattr(f, "_post_external_session_status", _record_status)

    iteration = {"n": 0}

    async def _sleep(_s):
        iteration["n"] += 1
        if iteration["n"] >= 3:
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

    assert ("status", "idle") in events
    assert ("item", 4) in events  # the final answer row was mirrored
    # The invariant under test: content first, completion signal second.
    assert events.index(("item", 4)) < events.index(("status", "idle"))
    assert events.count(("status", "idle")) == 1
    assert hstatus.read_posted_count(bridge_dir) == 1


# ---------------------------------------------------------------------------
# Live tool-call cards: per-turn response_id + running edge (issue #1874).
# ---------------------------------------------------------------------------


def _mi(msg_id: int, item_type: str, *, role: str | None = None) -> f._MirrorItem:
    """Build a mirror item like ``_read_new_items`` produces (per-row id)."""
    data: dict[str, object] = {}
    if role is not None:
        data["role"] = role
    return f._MirrorItem(
        msg_id=msg_id, item_type=item_type, item_data=data, response_id=f"hermes:{msg_id}"
    )


def test_annotate_shares_one_turn_id_and_emits_running_once() -> None:
    # user -> assistant+tool_call -> tool -> assistant(final): one turn.
    items = [
        _mi(1, "message", role="user"),
        _mi(2, "function_call"),
        _mi(3, "function_call_output"),
        _mi(4, "message", role="assistant"),
    ]
    actions, active = f._annotate_turn_actions(items, None)
    running = [a.response_id for a in actions if a.kind == "running"]
    assert running == ["hermes_turn_1"]  # exactly one running edge, at the open
    stamped = {a.item.response_id for a in actions if a.kind == "item" and a.item.item_type}
    assert stamped == {"hermes_turn_1"}  # every item shares the turn id
    assert active is None  # terminal assistant row closes the turn
    assert actions[-1].turn_id_after is None


def test_annotate_new_id_per_turn_across_polls() -> None:
    # Turn 1 completes in poll 1; turn 2 opens in poll 2 → a distinct id, and the
    # running edge fires once per turn (state carried via the returned active id).
    a1, active = f._annotate_turn_actions(
        [_mi(1, "message", role="user"), _mi(2, "message", role="assistant")], None
    )
    assert [a.response_id for a in a1 if a.kind == "running"] == ["hermes_turn_1"]
    assert active is None
    a2, active = f._annotate_turn_actions(
        [_mi(3, "message", role="user"), _mi(4, "message", role="assistant")], active
    )
    assert [a.response_id for a in a2 if a.kind == "running"] == ["hermes_turn_3"]
    assert active is None


def test_annotate_no_duplicate_running_mid_turn() -> None:
    # A turn split across polls: the opener is in poll 1, the rest in poll 2 with
    # the id carried in — no second running edge.
    a1, active = f._annotate_turn_actions(
        [_mi(1, "message", role="user"), _mi(2, "function_call")], None
    )
    assert [a.response_id for a in a1 if a.kind == "running"] == ["hermes_turn_1"]
    assert active == "hermes_turn_1"
    a2, active = f._annotate_turn_actions(
        [_mi(3, "function_call_output"), _mi(4, "message", role="assistant")], active
    )
    assert [a.kind for a in a2 if a.kind == "running"] == []  # no re-open
    assert {a.item.response_id for a in a2 if a.kind == "item" and a.item.item_type} == {
        "hermes_turn_1"
    }
    assert active is None


def test_annotate_abort_then_new_user_reopens() -> None:
    # Turn opens but never reaches a terminal row (abort); a new user row still
    # starts a fresh turn (id overwritten), proving no stuck state blocks it.
    a1, active = f._annotate_turn_actions(
        [_mi(1, "message", role="user"), _mi(2, "function_call")], None
    )
    assert [a.response_id for a in a1 if a.kind == "running"] == ["hermes_turn_1"]
    assert active == "hermes_turn_1"  # still in flight, no terminal seen
    a2, active = f._annotate_turn_actions([_mi(5, "message", role="user")], active)
    assert [a.response_id for a in a2 if a.kind == "running"] == ["hermes_turn_5"]
    assert active == "hermes_turn_5"


def test_annotate_recovers_missed_opener() -> None:
    # Forwarder starts mid-turn (no user opener in the batch); assistant activity
    # with no active turn mints an id and emits running so its cards still go live.
    items = [_mi(7, "function_call"), _mi(8, "function_call_output")]
    actions, active = f._annotate_turn_actions(items, None)
    assert [a.response_id for a in actions if a.kind == "running"] == ["hermes_turn_7"]
    assert active == "hermes_turn_7"


async def test_post_external_session_status_carries_response_id(tmp_path) -> None:
    client = _FakeClient()
    await f._post_external_session_status(
        client, session_id="conv_r", status="running", response_id="hermes_turn_9"
    )
    _url, body = client.posts[0]
    assert body["type"] == "external_session_status"
    assert body["data"] == {"status": "running", "response_id": "hermes_turn_9"}
    # idle omits response_id (server pops the active id on any idle).
    await f._post_external_session_status(client, session_id="conv_r", status="idle")
    _url, body = client.posts[1]
    assert body["data"] == {"status": "idle"}


def test_forward_state_active_turn_id_roundtrip(tmp_path: Path) -> None:
    state = f._ForwardState(
        hermes_session_id="s1", last_id=4, launch_epoch_s=1.0, active_turn_id="hermes_turn_4"
    )
    assert f._write_state(tmp_path, state) is True
    assert f._read_state(tmp_path).active_turn_id == "hermes_turn_4"


async def test_forward_loop_emits_running_then_idle_for_tool_call_turn(
    tmp_path, monkeypatch
) -> None:
    """End-to-end over the poll loop: a tool-call turn yields a running edge (with
    the turn id) followed by idle, and the mirrored items carry that same id."""
    workspace = str(tmp_path)
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO sessions(id, source, cwd, started_at) VALUES (?,?,?,?)",
        ("s1", "cli", workspace, 1000.0),
    )
    tc = json.dumps([{"id": "c1", "call_id": "c1", "function": {"name": "f", "arguments": "{}"}}])
    con.executemany(
        "INSERT INTO messages"
        "(session_id, role, content, tool_call_id, tool_calls, tool_name, active)"
        " VALUES (?,?,?,?,?,?,?)",
        [
            ("s1", "user", "do it", None, None, None, 1),
            ("s1", "assistant", "", None, tc, None, 1),
            ("s1", "tool", "result", "c1", None, "f", 1),
            ("s1", "assistant", "done", None, None, None, 1),
        ],
    )
    con.commit()
    con.close()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    statuses: list[tuple[str, str | None]] = []
    posted_items: list[f._MirrorItem] = []

    async def _record_item(_client, *, session_id, item):
        posted_items.append(item)

    async def _record_status(_client, *, session_id, status, response_id=None):
        statuses.append((status, response_id))

    monkeypatch.setattr(f, "_post_conversation_item", _record_item)
    monkeypatch.setattr(f, "_post_external_session_status", _record_status)

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
            session_id="conv_tc",
            bridge_dir=bridge_dir,
            agent_name="hermes-native-ui",
            workspace=workspace,
            launch_epoch_s=1000.0,
            db_path=db,
        )

    # running (with the turn id) before idle; the function_call item carries the
    # same id so the web renders the card live against the running edge. The
    # clean-close idle carries that same id so the web settles the exact card
    # (an id-less idle is a no-op there while the response is still streaming).
    assert ("running", "hermes_turn_1") in statuses
    assert statuses[0] == ("running", "hermes_turn_1")
    assert ("idle", "hermes_turn_1") in statuses
    assert statuses.index(("running", "hermes_turn_1")) < statuses.index(("idle", "hermes_turn_1"))
    fc = [it for it in posted_items if it.item_type == "function_call"]
    assert fc and all(it.response_id == "hermes_turn_1" for it in fc)


@pytest.mark.asyncio
async def test_forward_loop_reasserts_running_while_turn_in_flight(tmp_path, monkeypatch) -> None:
    """A turn that stays in flight across polls (no terminal row yet) re-posts its
    ``running`` edge each poll, so the runner's PTY-activity ``idle`` (fired when the
    pane goes quiet mid-tool) can't strand the live card. No ``idle`` is posted while
    the turn is unfinished, and the open poll does not double-post ``running``.

    This is deliberately also the abort-without-terminal-row behavior: from the
    store such an abort is indistinguishable from a silent tool, so the card stays
    live until a terminal row lands or the next user turn re-opens with a fresh id
    (see ``test_annotate_abort_then_new_user_reopens``)."""
    workspace = str(tmp_path)
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO sessions(id, source, cwd, started_at) VALUES (?,?,?,?)",
        ("s1", "cli", workspace, 1000.0),
    )
    tc = json.dumps([{"id": "c1", "call_id": "c1", "function": {"name": "f", "arguments": "{}"}}])
    # user + assistant+tool_call, but NO tool result / terminal assistant row: the
    # turn is still running (e.g. a long, silent `sleep`).
    con.executemany(
        "INSERT INTO messages"
        "(session_id, role, content, tool_call_id, tool_calls, tool_name, active)"
        " VALUES (?,?,?,?,?,?,?)",
        [
            ("s1", "user", "run a slow sleep", None, None, None, 1),
            ("s1", "assistant", "", None, tc, None, 1),
        ],
    )
    con.commit()
    con.close()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    statuses: list[tuple[str, str | None]] = []

    async def _record_item(_client, *, session_id, item):
        pass

    async def _record_status(_client, *, session_id, status, response_id=None):
        statuses.append((status, response_id))

    monkeypatch.setattr(f, "_post_conversation_item", _record_item)
    monkeypatch.setattr(f, "_post_external_session_status", _record_status)

    iteration = {"n": 0}

    async def _sleep(_s):
        iteration["n"] += 1
        if iteration["n"] >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(f.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await f.forward_hermes_store_to_session(
            base_url="http://x",
            headers={},
            session_id="conv_slow",
            bridge_dir=bridge_dir,
            agent_name="hermes-native-ui",
            workspace=workspace,
            launch_epoch_s=1000.0,
            db_path=db,
        )

    running = [s for s in statuses if s[0] == "running"]
    # Open poll posts running once (no re-assert that poll); subsequent polls with
    # no new rows re-assert it — so the turn's running edge is posted more than once.
    assert running[0] == ("running", "hermes_turn_1")
    assert running.count(("running", "hermes_turn_1")) >= 2
    # The turn never completed, so no idle is posted.
    assert not any(s[0] == "idle" for s in statuses)


async def _run_forward_over_seeded_rows(
    tmp_path, monkeypatch, rows: list[tuple], *, iterations: int = 2
) -> tuple[list[tuple[str, str | None]], list]:
    """Drive the poll loop once over a fully-seeded ``messages`` table.

    Returns ``(statuses, posted_items)`` captured from the stubbed sessions client.
    """
    workspace = str(tmp_path)
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO sessions(id, source, cwd, started_at) VALUES (?,?,?,?)",
        ("s1", "cli", workspace, 1000.0),
    )
    con.executemany(
        "INSERT INTO messages"
        "(session_id, role, content, tool_call_id, tool_calls, tool_name, active)"
        " VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    statuses: list[tuple[str, str | None]] = []
    posted_items: list = []

    async def _record_item(_client, *, session_id, item):
        posted_items.append(item)

    async def _record_status(_client, *, session_id, status, response_id=None):
        statuses.append((status, response_id))

    monkeypatch.setattr(f, "_post_conversation_item", _record_item)
    monkeypatch.setattr(f, "_post_external_session_status", _record_status)

    iteration = {"n": 0}

    async def _sleep(_s):
        iteration["n"] += 1
        if iteration["n"] >= iterations:
            raise asyncio.CancelledError

    monkeypatch.setattr(f.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await f.forward_hermes_store_to_session(
            base_url="http://x",
            headers={},
            session_id="conv_multi",
            bridge_dir=bridge_dir,
            agent_name="hermes-native-ui",
            workspace=workspace,
            launch_epoch_s=1000.0,
            db_path=db,
        )
    return statuses, posted_items


@pytest.mark.asyncio
async def test_forward_loop_sequential_two_tool_calls_share_one_turn_id(
    tmp_path, monkeypatch
) -> None:
    """A turn with two SEQUENTIAL tool calls (user → asst+tc1 → tool1 → asst+tc2 →
    tool2 → asst-final) stamps every function_call / output with the one turn id,
    opens the turn once, and closes it once at the terminal — the intermediate
    asst+tc2 step must NOT re-open a new turn."""
    tc1 = json.dumps([{"id": "c1", "call_id": "c1", "function": {"name": "f", "arguments": "{}"}}])
    tc2 = json.dumps([{"id": "c2", "call_id": "c2", "function": {"name": "g", "arguments": "{}"}}])
    rows = [
        ("s1", "user", "run two things", None, None, None, 1),
        ("s1", "assistant", "", None, tc1, None, 1),
        ("s1", "tool", "res1", "c1", None, "f", 1),
        ("s1", "assistant", "", None, tc2, None, 1),
        ("s1", "tool", "res2", "c2", None, "g", 1),
        ("s1", "assistant", "done", None, None, None, 1),
    ]
    statuses, posted_items = await _run_forward_over_seeded_rows(tmp_path, monkeypatch, rows)

    fc = [it for it in posted_items if it.item_type == "function_call"]
    fco = [it for it in posted_items if it.item_type == "function_call_output"]
    assert len(fc) == 2 and len(fco) == 2
    ids = {it.response_id for it in fc + fco}
    assert ids == {"hermes_turn_1"}
    # Opened exactly once (no re-open on the intermediate asst+tc2 step).
    assert [s for s in statuses if s[0] == "running"] == [("running", "hermes_turn_1")]
    # Closed once, carrying the turn id so the web settles the card deterministically.
    assert [s for s in statuses if s[0] == "idle"] == [("idle", "hermes_turn_1")]


@pytest.mark.asyncio
async def test_forward_loop_parallel_two_tool_calls_share_one_turn_id(
    tmp_path, monkeypatch
) -> None:
    """A turn whose single assistant row carries two PARALLEL tool_calls emits two
    function_call items (one per call), both stamped with the one turn id, alongside
    their two outputs — one running edge at open, one idle at close."""
    parallel = json.dumps(
        [
            {"id": "c1", "call_id": "c1", "function": {"name": "read", "arguments": '{"p":"A"}'}},
            {"id": "c2", "call_id": "c2", "function": {"name": "read", "arguments": '{"p":"B"}'}},
        ]
    )
    rows = [
        ("s1", "user", "read A and B", None, None, None, 1),
        ("s1", "assistant", "", None, parallel, None, 1),
        ("s1", "tool", "resA", "c1", None, "read", 1),
        ("s1", "tool", "resB", "c2", None, "read", 1),
        ("s1", "assistant", "done", None, None, None, 1),
    ]
    statuses, posted_items = await _run_forward_over_seeded_rows(tmp_path, monkeypatch, rows)

    fc = [it for it in posted_items if it.item_type == "function_call"]
    fco = [it for it in posted_items if it.item_type == "function_call_output"]
    assert len(fc) == 2 and len(fco) == 2
    assert {it.item_data["call_id"] for it in fc} == {"c1", "c2"}
    ids = {it.response_id for it in fc + fco}
    assert ids == {"hermes_turn_1"}
    assert [s for s in statuses if s[0] == "running"] == [("running", "hermes_turn_1")]
    assert [s for s in statuses if s[0] == "idle"] == [("idle", "hermes_turn_1")]


@pytest.mark.asyncio
async def test_forward_loop_running_edge_does_not_advance_cursor_before_item(
    tmp_path, monkeypatch
) -> None:
    """A crash between the turn's running edge and the opening row's item POST must
    NOT advance last_id. The running edge mirrors no message row; only the item
    POST advances the cursor, and only after it succeeds. Otherwise a restart reads
    an advanced last_id and _read_new_items (WHERE id > last_id) skips the opening
    user row, permanently dropping it from the mirrored session."""
    workspace = str(tmp_path)
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO sessions(id, source, cwd, started_at) VALUES (?,?,?,?)",
        ("s1", "cli", workspace, 1000.0),
    )
    con.execute(
        "INSERT INTO messages"
        "(session_id, role, content, tool_call_id, tool_calls, tool_name, active)"
        " VALUES (?,?,?,?,?,?,?)",
        ("s1", "user", "do it", None, None, None, 1),
    )
    con.commit()
    con.close()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    statuses: list[tuple[str, str | None]] = []

    async def _record_status(_client, *, session_id, status, response_id=None):
        statuses.append((status, response_id))

    async def _boom_item(_client, *, session_id, item):
        raise RuntimeError("simulated crash while POSTing the opening row")

    monkeypatch.setattr(f, "_post_external_session_status", _record_status)
    monkeypatch.setattr(f, "_post_conversation_item", _boom_item)

    iteration = {"n": 0}

    async def _sleep(_s):
        iteration["n"] += 1
        if iteration["n"] >= 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(f.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await f.forward_hermes_store_to_session(
            base_url="http://x",
            headers={},
            session_id="conv_crash",
            bridge_dir=bridge_dir,
            agent_name="hermes-native-ui",
            workspace=workspace,
            launch_epoch_s=1000.0,
            db_path=db,
        )

    # The running edge was posted (best-effort), but the item POST crashed — so
    # last_id must still be 0, letting a restart re-read the opening row.
    assert ("running", "hermes_turn_1") in statuses
    assert f._read_state(bridge_dir).last_id == 0


@pytest.mark.asyncio
async def test_forward_loop_empty_prose_terminal_closes_turn(tmp_path, monkeypatch) -> None:
    """An assistant terminal row with empty content (no prose, no tool_calls)
    yields a role-less sentinel, but must still CLOSE the turn: active_turn_id is
    cleared so the running re-assert stops. Otherwise the id leaks and the
    re-assert re-posts running forever, stranding the web card as live."""
    workspace = str(tmp_path)
    db = tmp_path / "state.db"
    con = sqlite3.connect(db)
    con.executescript(_SCHEMA)
    con.execute(
        "INSERT INTO sessions(id, source, cwd, started_at) VALUES (?,?,?,?)",
        ("s1", "cli", workspace, 1000.0),
    )
    tc = json.dumps([{"id": "c1", "call_id": "c1", "function": {"name": "f", "arguments": "{}"}}])
    con.executemany(
        "INSERT INTO messages"
        "(session_id, role, content, tool_call_id, tool_calls, tool_name, active)"
        " VALUES (?,?,?,?,?,?,?)",
        [
            ("s1", "user", "do it", None, None, None, 1),
            ("s1", "assistant", "", None, tc, None, 1),
            ("s1", "tool", "result", "c1", None, "f", 1),
            ("s1", "assistant", "", None, None, None, 1),  # empty-prose terminal
        ],
    )
    con.commit()
    con.close()
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()

    statuses: list[tuple[str, str | None]] = []

    async def _record_item(_client, *, session_id, item):
        pass

    async def _record_status(_client, *, session_id, status, response_id=None):
        statuses.append((status, response_id))

    monkeypatch.setattr(f, "_post_conversation_item", _record_item)
    monkeypatch.setattr(f, "_post_external_session_status", _record_status)

    iteration = {"n": 0}

    async def _sleep(_s):
        iteration["n"] += 1
        if iteration["n"] >= 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(f.asyncio, "sleep", _sleep)

    with pytest.raises(asyncio.CancelledError):
        await f.forward_hermes_store_to_session(
            base_url="http://x",
            headers={},
            session_id="conv_empty_terminal",
            bridge_dir=bridge_dir,
            agent_name="hermes-native-ui",
            workspace=workspace,
            launch_epoch_s=1000.0,
            db_path=db,
        )

    # The empty-prose terminal closed the turn: id cleared, so the re-assert
    # stops (exactly one running, at open — no perpetual re-post).
    assert f._read_state(bridge_dir).active_turn_id is None
    assert [s for s in statuses if s[0] == "running"] == [("running", "hermes_turn_1")]
    assert ("idle", "hermes_turn_1") in statuses
