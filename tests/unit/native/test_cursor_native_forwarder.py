"""Unit tests for the cursor-native TUI→web forwarder.

Covers the pure pieces a live cursor-agent isn't needed for: reading the
content-addressed SQLite chat store (including the live-WAL layout that the
``immutable=1`` open mode silently missed), unwrapping cursor's
``<user_query>`` framing, building conversation items, rowid-based dedup,
store discovery by ``md5(cwd)`` + launch recency, and the POST shapes. The live
tmux + cursor-agent path is exercised by the e2e gate, not here.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import httpx
import pytest

from omnigent import cursor_native_forwarder as fwd


def _make_store(
    path: Path, rows: list[tuple[str, object]], *, wal: bool = False
) -> sqlite3.Connection:
    """Create a cursor-like ``blobs`` store and return the (kept-open) writer.

    When *wal* is set the store is left in WAL mode with autocheckpoint
    disabled and the writer connection is returned open, so the committed rows
    live only in the ``-wal`` sidecar (the main db stays nearly empty) — the
    exact layout a live chat has and that ``immutable=1`` would fail to read.
    """
    con = sqlite3.connect(str(path))
    if wal:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA wal_autocheckpoint=0")
    con.execute("CREATE TABLE blobs(id TEXT PRIMARY KEY, data BLOB)")
    for blob_id, data in rows:
        payload = data if isinstance(data, bytes) else json.dumps(data).encode("utf-8")
        con.execute("INSERT INTO blobs(id, data) VALUES(?, ?)", (blob_id, payload))
    con.commit()
    return con


def _user(text: str) -> dict:
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _assistant(parts: list[dict]) -> dict:
    return {"role": "assistant", "content": parts}


class TestUnwrapUserQuery:
    def test_extracts_inner_prompt_and_strips_control_bytes(self) -> None:
        raw = "<user_query>\n\x01\x0bHi there?\n\n</user_query>"
        assert fwd._unwrap_user_query(raw) == "Hi there?"

    def test_context_dump_without_wrapper_is_skipped(self) -> None:
        assert fwd._unwrap_user_query("<user_info>\nOS Version: linux\n...") is None

    def test_empty_query_is_skipped(self) -> None:
        assert fwd._unwrap_user_query("<user_query>\n  \n</user_query>") is None

    def test_strips_injected_attachment_markers(self) -> None:
        raw = "<user_query>\n[Attached: /tmp/x/img.png]\ndescribe this\n</user_query>"
        assert fwd._unwrap_user_query(raw) == "describe this"


class TestContentText:
    def test_string_content(self) -> None:
        assert fwd._content_text("hello") == "hello"

    def test_part_list_joins_only_text_parts(self) -> None:
        parts = [
            {"type": "redacted-reasoning"},
            {"type": "text", "text": "A"},
            {"type": "text", "text": "B"},
        ]
        assert fwd._content_text(parts) == "AB"

    def test_unknown_content_is_empty(self) -> None:
        assert fwd._content_text({"weird": 1}) == ""


class TestBlobToItem:
    # _blob_to_item receives the raw blob payload (a JSON string, as stored).
    @staticmethod
    def _blob(obj: object) -> str:
        return json.dumps(obj)

    def test_user_query_becomes_input_text_item(self) -> None:
        item = fwd._blob_to_item(
            5, "bid", self._blob(_user("<user_query>\nhi\n</user_query>")), "cursor-native-ui"
        )
        assert item is not None
        assert item.item_type == "message"
        assert item.item_data == {
            "role": "user",
            "content": [{"type": "input_text", "text": "hi"}],
        }
        assert item.response_id == "cursor:bid"

    def test_assistant_text_becomes_output_text_item(self) -> None:
        item = fwd._blob_to_item(
            9,
            "bid",
            self._blob(
                _assistant([{"type": "redacted-reasoning"}, {"type": "text", "text": "answer"}])
            ),
            "agentx",
        )
        assert item is not None
        assert item.item_data == {
            "role": "assistant",
            "agent": "agentx",
            "content": [{"type": "output_text", "text": "answer"}],
        }

    def test_assistant_without_prose_is_skipped(self) -> None:
        # reasoning/tool-only turn with no text part → nothing to mirror
        assert (
            fwd._blob_to_item(
                9, "bid", self._blob(_assistant([{"type": "redacted-reasoning"}])), "a"
            )
            is None
        )

    def test_system_and_context_dump_are_skipped(self) -> None:
        assert (
            fwd._blob_to_item(1, "bid", self._blob({"role": "system", "content": "x"}), "a")
            is None
        )
        assert fwd._blob_to_item(2, "bid", self._blob(_user("<user_info>\nbig dump")), "a") is None

    def test_binary_merkle_node_is_skipped(self) -> None:
        assert fwd._blob_to_item(3, "bid", b"\n \x92\xc0\xa6w\xef&", "a") is None


class TestReadNewItems:
    def test_reads_live_wal_store(self, tmp_path: Path) -> None:
        # Regression: a live chat keeps its data in the -wal sidecar. The old
        # ``immutable=1`` open ignored the WAL and saw an empty db; mode=ro
        # must read it.
        store = tmp_path / "store.db"
        writer = _make_store(
            store,
            [
                ("s", {"role": "system", "content": "x"}),
                ("u", _user("<user_query>\nReply ALPHA\n</user_query>")),
                ("bin", b"\x00binary"),
                ("a", _assistant([{"type": "text", "text": "ALPHA"}])),
            ],
            wal=True,
        )
        try:
            # Sanity: the main db file really is near-empty (data is in -wal).
            assert (store.with_name("store.db-wal")).exists()
            items = fwd._read_new_items(store, 0, "cursor-native-ui")
        finally:
            writer.close()
        posted = [it for it in items if it.item_type]
        assert [it.item_data["role"] for it in posted] == ["user", "assistant"]
        assert posted[0].item_data["content"][0]["text"] == "Reply ALPHA"
        assert posted[1].item_data["content"][0]["text"] == "ALPHA"
        # Every row (incl. skipped system/binary) advances the cursor.
        assert max(it.rowid for it in items) == 4

    def test_rowid_dedup_skips_already_seen(self, tmp_path: Path) -> None:
        store = tmp_path / "store.db"
        writer = _make_store(
            store,
            [
                ("u", _user("<user_query>\nhi\n</user_query>")),
                ("a", _assistant([{"type": "text", "text": "yo"}])),
            ],
        )
        try:
            assert fwd._read_new_items(store, 0, "a")  # cold read sees both
            # last_rowid past the end → nothing new
            assert fwd._read_new_items(store, 2, "a") == []
        finally:
            writer.close()


class TestDiscoverStore:
    def _seed_chat(self, root: Path, workspace: str, chat_id: str, created_ms: int) -> Path:
        chat = root / hashlib.md5(workspace.encode()).hexdigest() / chat_id
        chat.mkdir(parents=True)
        (chat / "store.db").write_bytes(b"")
        (chat / "meta.json").write_text(json.dumps({"createdAtMs": created_ms}))
        return chat / "store.db"

    def test_picks_newest_chat_at_or_after_launch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fwd, "_cursor_chats_root", lambda: tmp_path)
        ws = "/home/u/proj"
        self._seed_chat(tmp_path, ws, "old", 1_000)
        newest = self._seed_chat(tmp_path, ws, "new", 5_000)
        assert fwd._discover_store(ws, launch_epoch_ms=4_000) == newest

    def test_excludes_chats_created_before_launch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fwd, "_cursor_chats_root", lambda: tmp_path)
        ws = "/home/u/proj"
        self._seed_chat(tmp_path, ws, "stale", 1_000)
        # launch is well after the only chat (beyond the skew) → no match
        assert fwd._discover_store(ws, launch_epoch_ms=1_000_000) is None

    def test_falls_back_across_workspace_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fwd, "_cursor_chats_root", lambda: tmp_path)
        # The chat lives under a DIFFERENT hash than md5(queried workspace)
        # (cursor normalized the path); with a SINGLE qualifying chat the
        # fallback unambiguously binds it.
        other = self._seed_chat(tmp_path, "/some/other/path", "c", 5_000)
        assert fwd._discover_store("/queried/workspace", launch_epoch_ms=4_000) == other

    def test_ambiguous_fallback_returns_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fwd, "_cursor_chats_root", lambda: tmp_path)
        # Two qualifying chats under different non-exact dirs → we can't tell
        # which session owns which, so bind nothing (avoid silent cross-talk).
        self._seed_chat(tmp_path, "/path/a", "c1", 5_000)
        self._seed_chat(tmp_path, "/path/b", "c2", 6_000)
        assert fwd._discover_store("/queried/workspace", launch_epoch_ms=4_000) is None


class TestStateRoundTrip:
    def test_write_then_read(self, tmp_path: Path) -> None:
        assert fwd._write_state(
            tmp_path, fwd._ForwardState(store_path="/x/store.db", last_rowid=7)
        )
        got = fwd._read_state(tmp_path)
        assert got.store_path == "/x/store.db"
        assert got.last_rowid == 7

    def test_cold_default_when_absent(self, tmp_path: Path) -> None:
        got = fwd._read_state(tmp_path)
        assert got.store_path is None
        assert got.last_rowid == 0

    def test_clear_removes_state(self, tmp_path: Path) -> None:
        fwd._write_state(tmp_path, fwd._ForwardState(store_path="/x/store.db", last_rowid=7))
        fwd.clear_cursor_bridge_state(tmp_path)
        assert fwd._read_state(tmp_path).store_path is None
        # idempotent: clearing an absent state must not raise
        fwd.clear_cursor_bridge_state(tmp_path)


class _RecordingClient:
    """Async httpx-client stub that records POSTs and returns HTTP 200."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, *, json: dict) -> httpx.Response:
        self.posts.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))


@pytest.mark.asyncio
async def test_post_conversation_item_shape() -> None:
    client = _RecordingClient()
    item = fwd._MirrorItem(
        rowid=5,
        item_type="message",
        item_data={"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        response_id="cursor:bid",
    )
    await fwd._post_conversation_item(client, session_id="conv_1", item=item)  # type: ignore[arg-type]
    url, body = client.posts[0]
    assert url == "/v1/sessions/conv_1/events"
    assert body["type"] == "external_conversation_item"
    assert body["data"] == {
        "item_type": "message",
        "item_data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
        "response_id": "cursor:bid",
    }
