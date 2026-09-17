"""Unit tests for the cursor-native TUI→web forwarder.

Covers the pure pieces a live cursor-agent isn't needed for: reading the
content-addressed SQLite chat store (including the live-WAL layout that the
``immutable=1`` open mode silently missed), unwrapping cursor's
``<user_query>`` framing, building conversation items, rowid-based dedup,
store discovery by ``md5(cwd)`` + launch recency, the POST shapes, and the
``external_session_id`` patch that enables cold resume. The live tmux +
cursor-agent path is exercised by the e2e gate, not here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from omnigent.harnesses.cursor_native import forwarder as fwd
from omnigent.harnesses.cursor_native.forwarder import _persist_native_compaction_item

# Real cursor chat ids are UUIDs. Use UUID-shaped ids in fixtures so the
# persist side (forwarder) and the resume side (runner's strict
# ``is_valid_cursor_chat_id`` guard) agree on the same id shape — exercising the
# persist→resume path with values the resume side would actually accept.
_CHAT_ID = "0ef42bbf-3b80-4bec-ac39-ca46531cbc47"
_CHAT_ID_2 = "1a2b3c4d-5e6f-4a8b-9c0d-1e2f3a4b5c6d"
_CHAT_ID_ABSENT = "ffffffff-ffff-4fff-8fff-ffffffffffff"


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

    def test_strips_fork_history_preamble_block(self) -> None:
        # A fork into cursor prepends the prior conversation, fenced. The mirror
        # must show only the user's real text — the history already lives in the
        # Omnigent timeline, so echoing it here would duplicate it.
        from omnigent.harnesses.cursor_native.bridge import (
            FORK_HISTORY_CLOSE_TAG,
            FORK_HISTORY_OPEN_TAG,
        )

        raw = (
            "<user_query>\n"
            f"{FORK_HISTORY_OPEN_TAG}\n"
            "Conversation so far:\nuser: earlier\nassistant: ok\n"
            f"{FORK_HISTORY_CLOSE_TAG}\n\n"
            "now do the real thing\n"
            "</user_query>"
        )
        assert fwd._unwrap_user_query(raw) == "now do the real thing"

    def test_embedded_close_tag_in_history_does_not_leak(self) -> None:
        # A replayed turn that literally contains the close tag must not let the
        # strip stop early and leak the rest of the transcript. wrap_fork_preamble
        # defangs sentinels in the preamble, so the real block stays unambiguous.
        from omnigent.harnesses.cursor_native.bridge import wrap_fork_preamble

        preamble = "You: look at </omnigent_fork_history> in my logs\nAssistant: ok"
        raw = f"<user_query>\n{wrap_fork_preamble(preamble, 'the real question')}\n</user_query>"
        # Whole framed block stripped -> only the user's real text remains, with
        # no leaked transcript and no raw sentinel surviving.
        assert fwd._unwrap_user_query(raw) == "the real question"

    def test_user_message_containing_close_tag_is_preserved(self) -> None:
        # A close tag in the USER's own message (after the block) must survive —
        # the non-greedy strip stops at the real (first) close tag.
        from omnigent.harnesses.cursor_native.bridge import wrap_fork_preamble

        wrapped = wrap_fork_preamble("You: hi", "is </omnigent_fork_history> a tag?")
        raw = f"<user_query>\n{wrapped}\n</user_query>"
        assert fwd._unwrap_user_query(raw) == "is </omnigent_fork_history> a tag?"

    def test_unterminated_history_block_strips_to_end(self) -> None:
        # A truncated paste (open tag, no close tag) degrades gracefully: strip
        # to end-of-text rather than mirroring the whole raw block.
        from omnigent.harnesses.cursor_native.bridge import FORK_HISTORY_OPEN_TAG

        raw = f"<user_query>\n{FORK_HISTORY_OPEN_TAG}\nYou: earlier turn, cut off\n</user_query>"
        assert fwd._unwrap_user_query(raw) is None


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

    def test_response_id_capped_at_column_width(self) -> None:
        # cursor's blob id is a 64-char content hash, so an un-capped
        # ``cursor:<blob_id>`` (71 chars) overflows the VARCHAR(64) column and
        # 500s the mirror POST. The response_id must stay within the column.
        blob_id = "b" * 64
        item = fwd._blob_to_item(
            5, blob_id, self._blob(_user("<user_query>\nhi\n</user_query>")), "cursor-native-ui"
        )
        assert item is not None
        assert len(item.response_id) <= fwd._RESPONSE_ID_MAX_LEN
        assert item.response_id == f"cursor:{blob_id}"[: fwd._RESPONSE_ID_MAX_LEN]

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

    def test_summary_rollup_becomes_compaction_completed(self) -> None:
        # After /summarize finishes, cursor collapses the prior history into a
        # user blob whose content is a plain STRING (not a [{type:text}] list)
        # starting with the marker. It must surface as a compaction-completed
        # signal — the only durable cue that the in-pane compaction finished —
        # not as a chat bubble.
        blob = self._blob(
            {"role": "user", "content": f"{fwd._COMPACTION_SUMMARY_PREFIX} Summary:\n1. ..."}
        )
        item = fwd._blob_to_item(12, "bid", blob, "cursor-native-ui")
        assert item is not None
        assert item.item_type == "compaction_completed"
        assert item.item_data == {}

    def test_plain_string_user_without_marker_is_skipped(self) -> None:
        # A bare-string user content that ISN'T the summary rollup has no
        # <user_query> wrapper, so it is neither a chat bubble nor a compaction
        # signal — skipped, exactly as before.
        blob = self._blob({"role": "user", "content": "just some unwrapped context"})
        assert fwd._blob_to_item(2, "bid", blob, "a") is None

    def test_automated_notification_with_user_query_is_skipped(self) -> None:
        content = (
            "<timestamp>Sunday, Aug 16, 2026, 10:49 AM (UTC-4)</timestamp>\n"
            "<system_notification>\n"
            "The following task has finished.\n"
            "</system_notification>\n"
            "<user_query>Briefly inform the user about the task result and perform "
            "any follow-up actions (if needed).</user_query>"
        )
        blob = self._blob({"role": "user", "content": content})
        assert fwd._blob_to_item(2, "bid", blob, "a") is None


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


class TestChatClaim:
    """``_chat_claimed_by_other`` keeps one cursor chat → one mirroring session.

    cursor keeps one chat per working dir, so two cursor-native sessions in the
    same cwd discover the same store; this guard stops both from mirroring it
    into two conversations (the duplicate-session bug).
    """

    def test_yields_to_earlier_live_session(self, tmp_path: Path) -> None:
        root = tmp_path / "cursor-native"
        earlier = root / "sessA"
        later = root / "sessB"
        earlier.mkdir(parents=True)
        later.mkdir(parents=True)
        store = "/cursor/chats/h/c/store.db"
        # The earlier-launched session claims the chat (fresh heartbeat on write).
        fwd._write_state(
            earlier, fwd._ForwardState(store_path=store, last_rowid=3, launch_epoch_ms=1_000)
        )
        # The later session must yield to the established one.
        assert fwd._chat_claimed_by_other(later, Path(store), my_launch_ms=2_000) is True
        # The earlier session does NOT yield, even once the later one has also
        # recorded a claim on the same chat.
        fwd._write_state(
            later, fwd._ForwardState(store_path=store, last_rowid=0, launch_epoch_ms=2_000)
        )
        assert fwd._chat_claimed_by_other(earlier, Path(store), my_launch_ms=1_000) is False

    def test_unrelated_store_is_not_claimed(self, tmp_path: Path) -> None:
        root = tmp_path / "cursor-native"
        (root / "sessA").mkdir(parents=True)
        (root / "sessB").mkdir(parents=True)
        fwd._write_state(
            root / "sessA",
            fwd._ForwardState(
                store_path="/cursor/chats/h/c1/store.db", last_rowid=1, launch_epoch_ms=1_000
            ),
        )
        # A session mirroring a DIFFERENT chat is not blocked.
        assert (
            fwd._chat_claimed_by_other(
                root / "sessB", Path("/cursor/chats/h/c2/store.db"), my_launch_ms=2_000
            )
            is False
        )

    def test_stale_sibling_claim_is_ignored(self, tmp_path: Path) -> None:
        root = tmp_path / "cursor-native"
        dead = root / "sessDead"
        live = root / "sessLive"
        dead.mkdir(parents=True)
        live.mkdir(parents=True)
        store = "/cursor/chats/h/c/store.db"
        # An ancient heartbeat marks a dead session; write the file directly so
        # _write_state does not refresh the heartbeat to "now".
        (dead / fwd._STATE_FILE).write_text(
            json.dumps(
                {"store_path": store, "last_rowid": 9, "launch_epoch_ms": 1_000, "heartbeat_ms": 1}
            ),
            encoding="utf-8",
        )
        assert fwd._chat_claimed_by_other(live, Path(store), my_launch_ms=2_000) is False


class _RecordingClient:
    """Async httpx-client stub that records POSTs and returns HTTP 200."""

    def __init__(self) -> None:
        self.posts: list[tuple[str, dict]] = []

    async def post(self, url: str, *, json: dict) -> httpx.Response:
        self.posts.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))


def _write_meta_model(con: sqlite3.Connection, model: str | None, *, key: str = "0") -> None:
    """Add cursor's ``meta`` table to *con* and store a hex-encoded model blob.

    Mirrors cursor's on-disk layout: ``meta(key TEXT, value TEXT)`` where value
    is hex-encoded JSON. When *model* is ``None`` the JSON omits ``lastUsedModel``.
    """
    con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    payload: dict = {"mode": "default"}
    if model is not None:
        payload["lastUsedModel"] = model
    hexed = json.dumps(payload).encode("utf-8").hex()
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, hexed))
    con.commit()


class TestLastUsedModelFromMetaValue:
    def test_decodes_hex_json(self) -> None:
        hexed = json.dumps({"lastUsedModel": "gpt-5.2"}).encode().hex()
        assert fwd._last_used_model_from_meta_value(hexed) == "gpt-5.2"

    def test_strips_whitespace(self) -> None:
        hexed = json.dumps({"lastUsedModel": "  composer-2.5  "}).encode().hex()
        assert fwd._last_used_model_from_meta_value(hexed) == "composer-2.5"

    def test_missing_field_is_none(self) -> None:
        hexed = json.dumps({"mode": "default"}).encode().hex()
        assert fwd._last_used_model_from_meta_value(hexed) is None

    def test_empty_model_is_none(self) -> None:
        hexed = json.dumps({"lastUsedModel": "   "}).encode().hex()
        assert fwd._last_used_model_from_meta_value(hexed) is None

    def test_non_hex_text_is_none(self) -> None:
        assert fwd._last_used_model_from_meta_value("not-hex-zzz") is None

    def test_bytes_value_is_decoded(self) -> None:
        raw = json.dumps({"lastUsedModel": "auto"}).encode()
        assert fwd._last_used_model_from_meta_value(raw) == "auto"


class TestReadLastUsedModel:
    def test_reads_model_from_live_wal_store(self, tmp_path: Path) -> None:
        store = tmp_path / "store.db"
        writer = _make_store(store, [("u", _user("<user_query>hi</user_query>"))], wal=True)
        try:
            _write_meta_model(writer, "claude-opus-4-7")
            assert fwd._read_last_used_model(store) == "claude-opus-4-7"
        finally:
            writer.close()

    def test_no_meta_table_is_none(self, tmp_path: Path) -> None:
        store = tmp_path / "store.db"
        writer = _make_store(store, [("u", _user("<user_query>hi</user_query>"))])
        try:
            assert fwd._read_last_used_model(store) is None
        finally:
            writer.close()


class TestPostModelChangeIfNew:
    @pytest.mark.asyncio
    async def test_first_observation_is_posted(self) -> None:
        # Unlike claude-native, cursor posts the FIRST observed model so an
        # un-pinned session shows the real cursor model instead of omnigent's
        # default ("fable") in the Web UI pill.
        client = _RecordingClient()
        state = fwd._ModelMirrorState()
        await fwd._post_model_change_if_new(
            client,  # type: ignore[arg-type]
            session_id="conv_1",
            state=state,
            model="claude-sonnet-4-5",
        )
        url, body = client.posts[0]
        assert url == "/v1/sessions/conv_1/events"
        assert body == {"type": "external_model_change", "data": {"model": "claude-sonnet-4-5"}}
        assert state.posted == "claude-sonnet-4-5"

    @pytest.mark.asyncio
    async def test_switch_after_seed_posts_external_model_change(self) -> None:
        client = _RecordingClient()
        state = fwd._ModelMirrorState(observed="composer-2.5", posted="composer-2.5")
        await fwd._post_model_change_if_new(
            client,  # type: ignore[arg-type]
            session_id="conv_1",
            state=state,
            model="gpt-5.2",
        )
        url, body = client.posts[0]
        assert url == "/v1/sessions/conv_1/events"
        assert body == {"type": "external_model_change", "data": {"model": "gpt-5.2"}}
        assert state.posted == "gpt-5.2"

    @pytest.mark.asyncio
    async def test_unchanged_model_does_not_repost(self) -> None:
        client = _RecordingClient()
        state = fwd._ModelMirrorState(observed="gpt-5.2", posted="gpt-5.2")
        await fwd._post_model_change_if_new(
            client,  # type: ignore[arg-type]
            session_id="conv_1",
            state=state,
            model="gpt-5.2",
        )
        assert client.posts == []

    @pytest.mark.asyncio
    async def test_none_observation_does_not_clear_or_post(self) -> None:
        client = _RecordingClient()
        state = fwd._ModelMirrorState(observed="gpt-5.2", posted="gpt-5.2")
        await fwd._post_model_change_if_new(
            client,  # type: ignore[arg-type]
            session_id="conv_1",
            state=state,
            model=None,
        )
        assert client.posts == []
        assert state.observed == "gpt-5.2"

    @pytest.mark.asyncio
    async def test_failed_post_retries_next_poll(self) -> None:
        class _FailingThenOkClient:
            def __init__(self) -> None:
                self.calls = 0

            async def post(self, url: str, *, json: dict) -> httpx.Response:
                self.calls += 1
                if self.calls == 1:
                    raise httpx.ConnectError("boom")
                return httpx.Response(200, request=httpx.Request("POST", url))

        client = _FailingThenOkClient()
        state = fwd._ModelMirrorState(observed="composer-2.5", posted="composer-2.5")
        # First poll: switch observed, POST fails → posted stays behind observed.
        await fwd._post_model_change_if_new(
            client,  # type: ignore[arg-type]
            session_id="conv_1",
            state=state,
            model="gpt-5.2",
        )
        assert state.posted == "composer-2.5" and state.observed == "gpt-5.2"
        # Next poll retries (model=None means "no fresh read") and succeeds.
        await fwd._post_model_change_if_new(
            client,  # type: ignore[arg-type]
            session_id="conv_1",
            state=state,
            model=None,
        )
        assert state.posted == "gpt-5.2"
        assert client.calls == 2


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


def _http_status_error(status: int) -> httpx.HTTPStatusError:
    """An ``HTTPStatusError`` carrying *status*, as ``raise_for_status`` would raise."""
    req = httpx.Request("POST", "http://test/v1/sessions/conv_1/events")
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=req, response=httpx.Response(status, request=req)
    )


class _FakePoster:
    """Async ``_post_conversation_item`` stub for driving the poll loop.

    ``fail(item)`` returns an exception to raise for that item (simulating a
    rejected or failed POST) or ``None`` to accept it. Every attempt lands in
    ``calls``; accepted items also land in ``delivered``.
    """

    def __init__(self, fail) -> None:
        self.calls: list[fwd._MirrorItem] = []
        self.delivered: list[fwd._MirrorItem] = []
        self._fail = fail

    async def __call__(self, client: object, *, session_id: str, item: fwd._MirrorItem) -> None:
        self.calls.append(item)
        exc = self._fail(item)
        if exc is not None:
            raise exc
        self.delivered.append(item)


async def _drive_forwarder(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    store: Path,
    poster: _FakePoster,
    *,
    until,
    max_ticks: int = 2000,
) -> Path:
    """Run the real poll loop against *store* + *poster* until *until* holds.

    Stubs discovery/claim so the loop binds *store* at once and routes every
    POST through *poster*, then polls ``until(bridge_dir)`` (which inspects the
    persisted cursor and/or *poster*) and cancels the loop. Raises if the
    condition is never reached within *max_ticks* — i.e. the loop wedged.
    """
    bridge_dir = tmp_path / "cursor-native" / "sess"
    bridge_dir.mkdir(parents=True)
    monkeypatch.setattr(fwd, "_discover_store", lambda workspace, launch_ms: store)
    monkeypatch.setattr(fwd, "_chat_claimed_by_other", lambda *a, **k: False)
    monkeypatch.setattr(fwd, "_post_conversation_item", poster)
    task = asyncio.create_task(
        fwd.forward_cursor_store_to_session(
            base_url="http://test",
            headers={},
            session_id="conv_1",
            bridge_dir=bridge_dir,
            agent_name="cursor-native-ui",
            workspace="/ws",
            launch_epoch_ms=1_000,
            poll_interval_s=0.001,
        )
    )
    try:
        for _ in range(max_ticks):
            if until(bridge_dir):
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("forwarder never reached the expected state (wedged?)")
    finally:
        task.cancel()
        # Drain the cancelled task (return_exceptions swallows its CancelledError).
        await asyncio.gather(task, return_exceptions=True)
    return bridge_dir


class TestForwardLoopPostFailures:
    """Drive the real poll loop to pin its POST-failure handling.

    The unit tests above cover the pure transforms; these exercise
    ``forward_cursor_store_to_session`` end to end against a fake poster, so the
    bounded-retry-then-skip guard — and the original truncation wedge it hardens
    against — are verified at the loop level, not just per item.
    """

    @staticmethod
    def _seed(store: Path, blobs: list[tuple[str, str]]) -> None:
        # Each (blob_id, prompt) becomes a user blob; rowids are 1, 2, … in order.
        writer = _make_store(
            store,
            [(bid, _user(f"<user_query>\n{text}\n</user_query>")) for bid, text in blobs],
        )
        writer.close()

    @pytest.mark.asyncio
    async def test_long_blob_id_is_mirrored_not_wedged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Incident repro: cursor's blob id is a 64-char content hash, so the
        # pre-fix ``cursor:<id>`` was 71 chars and overflowed the VARCHAR(64)
        # column — every mirror POST 500'd and the loop wedged on message #1. A
        # poster mimicking that column limit must now ACCEPT the capped id.
        store = tmp_path / "store.db"
        self._seed(store, [("a" * 64, "hello")])

        def fail(item: fwd._MirrorItem):
            if len(item.response_id) > fwd._RESPONSE_ID_MAX_LEN:
                return _http_status_error(500)
            return None

        poster = _FakePoster(fail)
        bridge = await _drive_forwarder(
            monkeypatch,
            tmp_path,
            store,
            poster,
            until=lambda b: fwd._read_state(b).last_rowid >= 1,
        )
        assert [it.rowid for it in poster.delivered] == [1]
        assert all(len(it.response_id) <= fwd._RESPONSE_ID_MAX_LEN for it in poster.delivered)
        assert fwd._read_state(bridge).last_rowid == 1

    @pytest.mark.asyncio
    async def test_rejected_item_is_skipped_after_bounded_retries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A persistently rejected item (rowid 2) must be retried only a BOUNDED
        # number of times and then skipped, so the messages after it still
        # mirror — no infinite re-post flood, no permanent wedge.
        store = tmp_path / "store.db"
        self._seed(store, [("b1", "one"), ("b2", "two"), ("b3", "three")])

        def fail(item: fwd._MirrorItem):
            return _http_status_error(500) if item.rowid == 2 else None

        poster = _FakePoster(fail)
        bridge = await _drive_forwarder(
            monkeypatch,
            tmp_path,
            store,
            poster,
            until=lambda b: fwd._read_state(b).last_rowid >= 3,
        )
        assert sum(it.rowid == 2 for it in poster.calls) == fwd._MAX_ITEM_POST_ATTEMPTS
        assert [it.rowid for it in poster.delivered] == [1, 3]
        assert fwd._read_state(bridge).last_rowid == 3

    @pytest.mark.asyncio
    async def test_ambiguous_failure_is_skipped_without_retry(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A ReadTimeout means the request was sent but the response was lost:
        # the server may have committed the item, and external items aren't
        # deduped, so a retry could duplicate the bubble. The loop must skip the
        # item after a SINGLE attempt — not the bounded-retry burst.
        store = tmp_path / "store.db"
        self._seed(store, [("b1", "one"), ("b2", "two")])
        req = httpx.Request("POST", "http://test")

        def fail(item: fwd._MirrorItem):
            return httpx.ReadTimeout("response lost", request=req) if item.rowid == 1 else None

        poster = _FakePoster(fail)
        bridge = await _drive_forwarder(
            monkeypatch,
            tmp_path,
            store,
            poster,
            until=lambda b: fwd._read_state(b).last_rowid >= 2,
        )
        assert sum(it.rowid == 1 for it in poster.calls) == 1
        assert [it.rowid for it in poster.delivered] == [2]
        assert fwd._read_state(bridge).last_rowid == 2

    @pytest.mark.asyncio
    async def test_connection_failure_retries_indefinitely(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A ConnectError means the server was unreachable — no bytes delivered,
        # not the item's fault. The loop must retry it indefinitely (NOT count it
        # toward the skip bound) so a server outage never drops a message.
        store = tmp_path / "store.db"
        self._seed(store, [("b1", "one")])
        req = httpx.Request("POST", "http://test")

        def fail(item: fwd._MirrorItem):
            return httpx.ConnectError("connection refused", request=req)

        poster = _FakePoster(fail)
        bridge = await _drive_forwarder(
            monkeypatch,
            tmp_path,
            store,
            poster,
            until=lambda b: len(poster.calls) >= fwd._MAX_ITEM_POST_ATTEMPTS + 3,
        )
        # Retried well past the skip bound, yet never advanced — not quarantined.
        assert fwd._read_state(bridge).last_rowid == 0
        assert not poster.delivered


# ---------------------------------------------------------------------------
# external_session_id patching (cold-resume support)
# ---------------------------------------------------------------------------


class _PatchRecordingClient:
    """Async stub that records PATCH calls and allows injecting a failure response."""

    def __init__(self, status: int = 200) -> None:
        self.patches: list[tuple[str, dict]] = []
        self._status = status

    async def patch(self, url: str, *, json: dict) -> httpx.Response:
        self.patches.append((url, json))
        return httpx.Response(self._status, request=httpx.Request("PATCH", url))


@pytest.mark.asyncio
async def test_patch_external_session_id_request_shape() -> None:
    """PATCH carries the correct URL and JSON body."""
    client = _PatchRecordingClient()
    await fwd._patch_external_session_id(client, session_id="conv_abc", chat_id=_CHAT_ID)  # type: ignore[arg-type]
    assert len(client.patches) == 1
    url, body = client.patches[0]
    assert url == "/v1/sessions/conv_abc"
    assert body == {"external_session_id": _CHAT_ID}


@pytest.mark.asyncio
async def test_patch_external_session_id_4xx_logs_but_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 4xx rejection from the server is logged but must not propagate."""
    client = _PatchRecordingClient(status=400)
    import logging

    with caplog.at_level(logging.WARNING):
        await fwd._patch_external_session_id(client, session_id="conv_x", chat_id="cid")  # type: ignore[arg-type]
    assert any("400" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_patch_external_session_id_http_error_logs_but_does_not_raise(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A transport error is logged and swallowed so the forwarder loop continues."""

    class _ErrorClient:
        async def patch(self, url: str, *, json: dict) -> httpx.Response:
            raise httpx.ConnectError("refused", request=httpx.Request("PATCH", url))

    import logging

    with caplog.at_level(logging.WARNING):
        await fwd._patch_external_session_id(_ErrorClient(), session_id="conv_x", chat_id="cid")  # type: ignore[arg-type]
    assert caplog.records


class TestPreseedResumeState:
    """``preseed_resume_state`` pre-seeds bridge state for cold resume."""

    def _seed_chat(self, chats_root: Path, workspace: str, chat_id: str, rows: int = 3) -> Path:
        import hashlib

        ws_hash = hashlib.md5(workspace.encode()).hexdigest()
        chat_dir = chats_root / ws_hash / chat_id
        chat_dir.mkdir(parents=True)
        store = chat_dir / "store.db"
        writer = _make_store(
            store,
            [(f"b{i}", _user(f"<user_query>\nmsg{i}\n</user_query>")) for i in range(rows)],
        )
        writer.close()
        return store

    def test_returns_false_when_store_absent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fwd, "_cursor_chats_root", lambda: tmp_path / "chats")
        bridge_dir = tmp_path / "bridge"
        bridge_dir.mkdir()
        result = fwd.preseed_resume_state(bridge_dir, "/ws", _CHAT_ID_ABSENT, 1_000)
        assert result is False
        assert fwd._read_state(bridge_dir).store_path is None

    def test_writes_store_path_and_current_rowid(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fwd, "_cursor_chats_root", lambda: tmp_path / "chats")
        store = self._seed_chat(tmp_path / "chats", "/ws", _CHAT_ID, rows=5)
        bridge_dir = tmp_path / "bridge"
        bridge_dir.mkdir()

        result = fwd.preseed_resume_state(bridge_dir, "/ws", _CHAT_ID, launch_epoch_ms=99_000)

        assert result is True
        state = fwd._read_state(bridge_dir)
        assert state.store_path == str(store)
        assert state.last_rowid == 5  # all 5 rows already in store
        assert state.launch_epoch_ms == 99_000

    def test_empty_store_seeds_rowid_zero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(fwd, "_cursor_chats_root", lambda: tmp_path / "chats")
        self._seed_chat(tmp_path / "chats", "/ws", _CHAT_ID_2, rows=0)
        bridge_dir = tmp_path / "bridge"
        bridge_dir.mkdir()

        fwd.preseed_resume_state(bridge_dir, "/ws", _CHAT_ID_2, launch_epoch_ms=1_000)

        assert fwd._read_state(bridge_dir).last_rowid == 0


class TestForwardLoopPreseedResume:
    """Forwarder uses pre-seeded bridge state on cold resume, skipping discovery."""

    @pytest.mark.asyncio
    async def test_uses_preseed_store_without_discover(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When bridge state is pre-seeded, forwarder skips _discover_store."""
        chat_id = _CHAT_ID
        store = tmp_path / chat_id / "store.db"
        store.parent.mkdir(parents=True)
        writer = _make_store(
            store,
            [
                ("old", _user("<user_query>\nold message\n</user_query>")),  # rowid 1 (pre-resume)
                ("new", _user("<user_query>\nnew message\n</user_query>")),  # rowid 2 (new)
            ],
        )
        writer.close()

        bridge_dir = tmp_path / "cursor-native" / "sess"
        bridge_dir.mkdir(parents=True)
        # Pre-seed: rowid 1 already mirrored (old history), start from there.
        fwd._write_state(
            bridge_dir,
            fwd._ForwardState(store_path=str(store), last_rowid=1, launch_epoch_ms=999_000),
        )

        discover_calls: list = []

        def _no_discover(workspace: str, launch_ms: int) -> None:
            discover_calls.append((workspace, launch_ms))
            return  # should never be reached

        monkeypatch.setattr(fwd, "_discover_store", _no_discover)
        monkeypatch.setattr(fwd, "_chat_claimed_by_other", lambda *a, **k: False)

        delivered: list[fwd._MirrorItem] = []

        async def _collect(client: object, *, session_id: str, item: fwd._MirrorItem) -> None:
            delivered.append(item)

        monkeypatch.setattr(fwd, "_post_conversation_item", _collect)
        monkeypatch.setattr(fwd, "_patch_external_session_id", lambda *a, **k: None)

        task = asyncio.create_task(
            fwd.forward_cursor_store_to_session(
                base_url="http://test",
                headers={},
                session_id="conv_1",
                bridge_dir=bridge_dir,
                agent_name="cursor-native-ui",
                workspace="/ws",
                launch_epoch_ms=1_000_000,  # far future — discovery would find nothing
                poll_interval_s=0.001,
            )
        )
        try:
            for _ in range(2000):
                if delivered:
                    break
                await asyncio.sleep(0.001)
            else:
                raise AssertionError("forwarder never mirrored the new message")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        # Only the NEW message (rowid 2) was mirrored; old history was skipped.
        assert len(delivered) == 1
        assert delivered[0].item_data["content"][0]["text"] == "new message"
        # _discover_store was never called (pre-seed took the fast path).
        assert not discover_calls


class TestForwardLoopExternalSessionId:
    """The poll loop patches external_session_id once when the store is found."""

    @staticmethod
    def _seed(store: Path, text: str = "hello") -> None:
        writer = _make_store(store, [("u", _user(f"<user_query>\n{text}\n</user_query>"))])
        writer.close()

    @pytest.mark.asyncio
    async def test_patches_chat_id_on_first_store_discovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The forwarder PATCHes external_session_id with the cursor chat_id."""
        store = tmp_path / _CHAT_ID / "store.db"
        store.parent.mkdir(parents=True)
        self._seed(store)

        patches: list[tuple[str, dict]] = []

        async def _fake_patch(client: object, *, session_id: str, chat_id: str) -> None:
            patches.append((session_id, chat_id))

        monkeypatch.setattr(fwd, "_patch_external_session_id", _fake_patch)
        monkeypatch.setattr(fwd, "_post_conversation_item", _FakePoster(lambda _: None))

        bridge_dir = tmp_path / "cursor-native" / "sess"
        bridge_dir.mkdir(parents=True)
        monkeypatch.setattr(fwd, "_discover_store", lambda ws, launch_ms: store)
        monkeypatch.setattr(fwd, "_chat_claimed_by_other", lambda *a, **k: False)

        task = asyncio.create_task(
            fwd.forward_cursor_store_to_session(
                base_url="http://test",
                headers={},
                session_id="conv_1",
                bridge_dir=bridge_dir,
                agent_name="cursor-native-ui",
                workspace="/ws",
                launch_epoch_ms=1_000,
                poll_interval_s=0.001,
            )
        )
        try:
            for _ in range(2000):
                if patches:
                    break
                await asyncio.sleep(0.001)
            else:
                raise AssertionError("external_session_id patch was never called")
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert len(patches) == 1
        session_id, chat_id = patches[0]
        assert session_id == "conv_1"
        assert chat_id == _CHAT_ID

    @pytest.mark.asyncio
    async def test_patches_only_once_across_multiple_polls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """external_session_id is patched exactly once, not on every poll tick."""
        store = tmp_path / _CHAT_ID_2 / "store.db"
        store.parent.mkdir(parents=True)
        self._seed(store)

        patch_count = 0

        async def _count_patches(client: object, *, session_id: str, chat_id: str) -> None:
            nonlocal patch_count
            patch_count += 1

        monkeypatch.setattr(fwd, "_patch_external_session_id", _count_patches)
        monkeypatch.setattr(fwd, "_post_conversation_item", _FakePoster(lambda _: None))

        bridge_dir = tmp_path / "cursor-native" / "sess"
        bridge_dir.mkdir(parents=True)
        monkeypatch.setattr(fwd, "_discover_store", lambda ws, launch_ms: store)
        monkeypatch.setattr(fwd, "_chat_claimed_by_other", lambda *a, **k: False)

        task = asyncio.create_task(
            fwd.forward_cursor_store_to_session(
                base_url="http://test",
                headers={},
                session_id="conv_2",
                bridge_dir=bridge_dir,
                agent_name="cursor-native-ui",
                workspace="/ws",
                launch_epoch_ms=1_000,
                poll_interval_s=0.001,
            )
        )
        try:
            # Let the loop run for several ticks after the patch fires.
            for _ in range(2000):
                if patch_count >= 1:
                    break
                await asyncio.sleep(0.001)
            # Extra ticks to confirm it doesn't re-patch.
            for _ in range(50):
                await asyncio.sleep(0.001)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        assert patch_count == 1


class TestCompactionCompletedForwarding:
    """The forwarder maps cursor's post-/summarize rollup blob to a
    ``external_compaction_status`` 'completed' edge.

    The runner raises the web UI's "Compacting…" spinner when it submits
    ``/summarize`` but cannot tell when cursor actually finishes (cursor-agent
    has no compaction hook). The forwarder closes that gap: when it tails the
    summary rollup blob out of the store it posts the completion, so the
    permanent "Conversation compacted" marker tracks cursor's real progress
    instead of flashing the instant the command was submitted.
    """

    @pytest.mark.asyncio
    async def test_summary_blob_posts_compaction_completed_not_a_bubble(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Store: a normal user turn (rowid 1) then the post-/summarize rollup
        # (rowid 2, a plain-string user blob with the marker prefix).
        store = tmp_path / "store.db"
        writer = _make_store(
            store,
            [
                ("b1", _user("<user_query>\nhi\n</user_query>")),
                (
                    "b2",
                    {
                        "role": "user",
                        "content": f"{fwd._COMPACTION_SUMMARY_PREFIX} Summary:\n1. ...",
                    },
                ),
            ],
        )
        writer.close()

        completions: list[str] = []

        async def _fake_compaction(client: object, *, session_id: str, status: str) -> None:
            completions.append(status)

        monkeypatch.setattr(fwd, "_post_external_compaction_status", _fake_compaction)

        poster = _FakePoster(lambda item: None)
        bridge = await _drive_forwarder(
            monkeypatch,
            tmp_path,
            store,
            poster,
            until=lambda b: bool(completions) and fwd._read_state(b).last_rowid >= 2,
        )

        # The rollup fired exactly one 'completed' edge — the web UI marker.
        assert completions == ["completed"]
        # It was NOT mirrored as a chat bubble (only the real user turn was).
        assert [it.rowid for it in poster.delivered] == [1]
        assert all(it.item_type == "message" for it in poster.delivered)
        # The cursor advanced past the rollup so it never re-fires completion.
        assert fwd._read_state(bridge).last_rowid == 2

    @pytest.mark.asyncio
    async def test_failed_completion_post_does_not_wedge_the_mirror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A failed completion POST must not wedge the loop: unlike a chat item it
        # carries no content to lose, so the cursor advances past it regardless
        # (the spinner just lingers). A later message must still mirror.
        store = tmp_path / "store.db"
        writer = _make_store(
            store,
            [
                (
                    "b1",
                    {
                        "role": "user",
                        "content": f"{fwd._COMPACTION_SUMMARY_PREFIX} Summary:\n1. ...",
                    },
                ),
                ("b2", _user("<user_query>\nnext\n</user_query>")),
            ],
        )
        writer.close()

        async def _failing_compaction(client: object, *, session_id: str, status: str) -> None:
            raise _http_status_error(500)

        monkeypatch.setattr(fwd, "_post_external_compaction_status", _failing_compaction)

        poster = _FakePoster(lambda item: None)
        bridge = await _drive_forwarder(
            monkeypatch,
            tmp_path,
            store,
            poster,
            until=lambda b: fwd._read_state(b).last_rowid >= 2,
        )

        # The message after the (failed) completion still mirrored, and the
        # cursor advanced past both — no wedge, no infinite re-post.
        assert [it.rowid for it in poster.delivered] == [2]
        assert fwd._read_state(bridge).last_rowid == 2


# ---------------------------------------------------------------------------
# _persist_native_compaction_item
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_native_compaction_item_posts_compaction_event() -> None:
    """Compaction event is posted with last_item_id and compacted_messages."""
    client = MagicMock()
    get_resp = MagicMock()
    get_resp.json.return_value = {"data": [{"id": "item_789"}]}
    get_resp.raise_for_status = MagicMock()
    client.get = AsyncMock(return_value=get_resp)

    post_resp = MagicMock()
    post_resp.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=post_resp)

    fake_rows = [
        (1, "b1", '{"role":"user","content":[{"type":"text","text":"hello"}]}'),
        (2, "b2", '{"role":"assistant","content":[{"type":"text","text":"hi back"}]}'),
    ]

    with patch.object(fwd, "_read_blob_rows", return_value=fake_rows):
        await _persist_native_compaction_item(
            client, session_id="conv_cursor", store_path=Path("/fake")
        )

    client.post.assert_called_once()
    _url, kwargs = client.post.call_args
    body = kwargs["json"]
    assert body["type"] == "compaction"
    assert body["data"]["last_item_id"] == "item_789"
    assert len(body["data"]["compacted_messages"]) == 2
    assert body["data"]["compacted_messages"][0]["role"] == "user"
    assert body["data"]["compacted_messages"][1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_persist_native_compaction_item_no_store_skips_messages() -> None:
    """When the store can't be read, POST has no compacted_messages key."""
    client = MagicMock()
    get_resp = MagicMock()
    get_resp.json.return_value = {"data": [{"id": "item_abc"}]}
    get_resp.raise_for_status = MagicMock()
    client.get = AsyncMock(return_value=get_resp)

    post_resp = MagicMock()
    post_resp.raise_for_status = MagicMock()
    client.post = AsyncMock(return_value=post_resp)

    with patch.object(fwd, "_read_blob_rows", side_effect=sqlite3.Error("no db")):
        await _persist_native_compaction_item(
            client, session_id="conv_cursor", store_path=Path("/fake")
        )

    client.post.assert_called_once()
    _url, kwargs = client.post.call_args
    body = kwargs["json"]
    assert body["type"] == "compaction"
    assert body["data"]["last_item_id"] == "item_abc"
    assert "compacted_messages" not in body["data"]


# ---------------------------------------------------------------------------
# ``/clear`` rotation (the pane starts a sibling chat)
# ---------------------------------------------------------------------------

#: Resource id the runner registers the cursor pane under.
_TERMINAL_ID = "terminal_cursor_main"


def _seed_rotation_chat(
    chats_root: Path,
    workspace: str,
    chat_id: str,
    created_ms: int,
    prompts: tuple[str, ...] = (),
    *,
    model: str | None = None,
) -> Path:
    """Create a cursor chat dir (``store.db`` + ``meta.json``) and return the store."""
    return _seed_rotation_chat_in_dir(
        chats_root / hashlib.md5(workspace.encode()).hexdigest(),
        chat_id,
        created_ms,
        prompts,
        model=model,
    )


def _seed_rotation_chat_in_dir(
    hash_dir: Path,
    chat_id: str,
    created_ms: int,
    prompts: tuple[str, ...] = (),
    *,
    model: str | None = None,
) -> Path:
    """Seed one chat under an explicitly named hash dir; return its store path."""
    chat = hash_dir / chat_id
    chat.mkdir(parents=True)
    writer = _make_store(
        chat / "store.db",
        [
            (f"{chat_id}-b{i}", _user(f"<user_query>\n{text}\n</user_query>"))
            for i, text in enumerate(prompts)
        ],
    )
    if model is not None:
        _write_meta_model(writer, model)
    writer.close()
    (chat / "meta.json").write_text(json.dumps({"createdAtMs": created_ms}), encoding="utf-8")
    return chat / "store.db"


class TestDetectRotatedChat:
    """``_detect_rotated_chat`` spots the sibling chat a ``/clear`` created.

    The bound ``store.db`` survives a ``/clear``, so the loop's only re-discovery
    trigger (a vanished store) never fires. These pin the successor scan — and,
    just as importantly, every case where it must stay silent, since a false
    positive rotates the session onto the wrong chat.
    """

    def _root(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        root = tmp_path / "chats"
        monkeypatch.setattr(fwd, "_cursor_chats_root", lambda: root)
        return root

    def test_detects_newer_sibling_chat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = self._root(tmp_path, monkeypatch)
        bound = _seed_rotation_chat(root, "/ws", _CHAT_ID, 1_000, ("before",))
        rotated = _seed_rotation_chat(root, "/ws", _CHAT_ID_2, 2_000, ("after",))
        assert fwd._detect_rotated_chat(bound_store=bound, launch_epoch_ms=1_000) == rotated

    def test_ignores_older_sibling_chat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = self._root(tmp_path, monkeypatch)
        bound = _seed_rotation_chat(root, "/ws", _CHAT_ID, 2_000, ("bound",))
        _seed_rotation_chat(root, "/ws", _CHAT_ID_2, 1_000, ("older",))
        assert fwd._detect_rotated_chat(bound_store=bound, launch_epoch_ms=1_000) is None

    def test_ignores_bound_chat_itself(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = self._root(tmp_path, monkeypatch)
        bound = _seed_rotation_chat(root, "/ws", _CHAT_ID, 2_000, ("bound",))
        assert fwd._detect_rotated_chat(bound_store=bound, launch_epoch_ms=1_000) is None

    def test_ignores_chat_created_before_launch_epoch(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A cold resume legitimately binds a chat created BEFORE this launch, so
        # "newer than bound" alone would let an older bystander chat impersonate a
        # rotation; the launch-epoch floor (minus the skew) is what rules it out.
        root = self._root(tmp_path, monkeypatch)
        bound = _seed_rotation_chat(root, "/ws", _CHAT_ID, 1_000, ("resumed",))
        _seed_rotation_chat(root, "/ws", _CHAT_ID_2, 2_000, ("bystander",))
        assert fwd._detect_rotated_chat(bound_store=bound, launch_epoch_ms=100_000) is None

    def test_ignores_chat_missing_meta_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``_chat_created_ms`` returns 0 without ``meta.json``, so a half-written
        # chat dir is invisible until cursor finishes creating it.
        root = self._root(tmp_path, monkeypatch)
        bound = _seed_rotation_chat(root, "/ws", _CHAT_ID, 1_000, ("bound",))
        rotated = _seed_rotation_chat(root, "/ws", _CHAT_ID_2, 2_000, ("after",))
        (rotated.parent / "meta.json").unlink()
        assert fwd._detect_rotated_chat(bound_store=bound, launch_epoch_ms=1_000) is None

    def test_ignores_chat_missing_store_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        root = self._root(tmp_path, monkeypatch)
        bound = _seed_rotation_chat(root, "/ws", _CHAT_ID, 1_000, ("bound",))
        rotated = _seed_rotation_chat(root, "/ws", _CHAT_ID_2, 2_000, ("after",))
        rotated.unlink()
        assert fwd._detect_rotated_chat(bound_store=bound, launch_epoch_ms=1_000) is None

    def test_ignores_empty_new_chat(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        # The TUI creates the chat dir lazily on the first message, and meta.json
        # can land before the first blob. Rotating onto a row-less store would
        # hand the new conversation a chat that may still be abandoned.
        root = self._root(tmp_path, monkeypatch)
        bound = _seed_rotation_chat(root, "/ws", _CHAT_ID, 1_000, ("bound",))
        _seed_rotation_chat(root, "/ws", _CHAT_ID_2, 2_000, ())
        assert fwd._detect_rotated_chat(bound_store=bound, launch_epoch_ms=1_000) is None

    def test_ignores_other_workspace_hash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Deliberately NOT ``_discover_store``: its cross-hash fallback exists for
        # a first bind under a path-hash mismatch and would adopt an unrelated
        # workspace's chat as "our" rotation.
        root = self._root(tmp_path, monkeypatch)
        bound = _seed_rotation_chat(root, "/ws", _CHAT_ID, 1_000, ("bound",))
        _seed_rotation_chat(root, "/other/ws", _CHAT_ID_2, 2_000, ("stranger",))
        assert fwd._detect_rotated_chat(bound_store=bound, launch_epoch_ms=1_000) is None

    def test_detects_sibling_under_a_hash_dir_no_workspace_would_compute(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The path-hash fallback binds stores whose hash dir is NOT
        # ``md5(workspace)``. Seeding under a name no md5 could produce pins the
        # scan to the bound store's OWN dir — those binds must stay rotatable.
        root = self._root(tmp_path, monkeypatch)
        bound = _seed_rotation_chat_in_dir(root / "not-an-md5-hash", _CHAT_ID, 1_000, ("bound",))
        rotated = _seed_rotation_chat_in_dir(
            root / "not-an-md5-hash", _CHAT_ID_2, 2_000, ("after",)
        )
        assert fwd._detect_rotated_chat(bound_store=bound, launch_epoch_ms=1_000) == rotated


class _APServer:
    """Mock Omnigent server for the rotation handshake and the mirror POSTs.

    Records every ``(method, path, body)`` so a test can assert the handshake
    *order*, and counts ``POST /v1/sessions`` separately because "exactly one
    replacement session" is the property that keeps a rotation from storming.
    """

    def __init__(
        self,
        *,
        old: str = "conv_old",
        new: str = "conv_new",
        runner_id: str | None = "runner_one",
        labels: dict[str, str] | None = None,
        snapshot_extra: dict[str, object] | None = None,
        create_status: int = 201,
        fail_old_item_posts: bool = False,
    ) -> None:
        self.old = old
        self.new = new
        self._runner_id = runner_id
        self._labels = {"omnigent.ui": "terminal"} if labels is None else labels
        self._snapshot_extra = snapshot_extra or {}
        self._create_status = create_status
        self._fail_old_item_posts = fail_old_item_posts
        self.calls: list[tuple[str, str, dict | None]] = []
        self.unexpected: list[tuple[str, str]] = []
        self.create_attempts = 0

    @property
    def paths(self) -> list[tuple[str, str]]:
        return [(method, path) for method, path, _ in self.calls]

    def bodies(self, method: str, path: str) -> list[dict | None]:
        return [b for m, p, b in self.calls if (m, p) == (method, path)]

    def event_types(self, session_id: str) -> list[str]:
        return [
            str(body.get("type"))
            for body in self.bodies("POST", f"/v1/sessions/{session_id}/events")
            if isinstance(body, dict)
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Answer one request, recording it first."""
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        method, path = request.method, request.url.path
        self.calls.append((method, path, body))
        if method == "GET" and path == f"/v1/sessions/{self.old}":
            snapshot: dict[str, object] = {
                "id": self.old,
                "agent_id": "ag_cursor",
                "labels": dict(self._labels),
            }
            if self._runner_id:
                snapshot["runner_id"] = self._runner_id
            snapshot.update(self._snapshot_extra)
            return httpx.Response(200, json=snapshot)
        if method == "POST" and path == "/v1/sessions":
            self.create_attempts += 1
            if self._create_status >= 400:
                return httpx.Response(self._create_status, json={"error": {"message": "boom"}})
            return httpx.Response(self._create_status, json={"id": self.new})
        if method == "PATCH" and path in (
            f"/v1/sessions/{self.old}",
            f"/v1/sessions/{self.new}",
        ):
            return httpx.Response(200, json={"id": path.rsplit("/", 1)[-1]})
        if (
            method == "POST"
            and path == f"/v1/sessions/{self.old}/resources/terminals/{_TERMINAL_ID}/transfer"
        ):
            return httpx.Response(200, json={"id": _TERMINAL_ID})
        if method == "POST" and path.endswith("/events"):
            if self._fail_old_item_posts and path == f"/v1/sessions/{self.old}/events":
                # A connection-level failure keeps ``retrying_items`` set for
                # good, which is exactly the state the rotation must not run in.
                raise httpx.ConnectError("server unreachable", request=request)
            return httpx.Response(200, json={"queued": False, "item_id": "item_x"})
        self.unexpected.append((method, path))
        return httpx.Response(404, json={"error": {"message": "unrouted"}})


class _RotationHarness:
    """Runs the real forwarder loop against a real chats tree and ``_APServer``.

    Builds the bridge dir the way production does (``bridge.json`` present, so
    ``read_active_session_id`` / ``write_active_session_id`` work), pre-seeds the
    forwarder cursor onto the bound chat so the loop binds it on the first poll,
    and routes the loop's own ``httpx.AsyncClient`` through a ``MockTransport``
    so the rotation handshake is observed as real HTTP rather than stubbed out.
    """

    workspace = "/ws"
    launch_epoch_ms = 1_000

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, server: _APServer) -> None:
        from omnigent.harnesses.cursor_native import bridge as cursor_bridge

        self.server = server
        self.chats_root = tmp_path / "chats"
        self.bridge_root = tmp_path / "omnigent-test" / "cursor-native"
        monkeypatch.setattr(fwd, "_cursor_chats_root", lambda: self.chats_root)
        monkeypatch.setattr(cursor_bridge, "_BRIDGE_ROOT", self.bridge_root)
        self.bridge = cursor_bridge
        self.bridge_dir = cursor_bridge.bridge_dir_for_session_id(server.old)
        cursor_bridge.write_mcp_bridge_config(self.bridge_dir)
        cursor_bridge.write_active_session_id(self.bridge_dir, server.old)
        self.bound_store: Path | None = None
        self.rotated_store: Path | None = None
        self.rotations: list[tuple[str, str]] = []
        self.polls = 0

        real_client = fwd.httpx.AsyncClient

        def _client_factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
            kwargs.pop("transport", None)
            return real_client(  # type: ignore[return-value]
                *args, transport=httpx.MockTransport(server.handler), **kwargs
            )

        monkeypatch.setattr(fwd.httpx, "AsyncClient", _client_factory)

        real_count = fwd.cursor_native_status.count_turn_ends

        def _counting_turn_ends(bridge_dir: Path) -> int:
            # Called once at the top of every poll — a deterministic poll clock.
            self.polls += 1
            return real_count(bridge_dir)

        monkeypatch.setattr(fwd.cursor_native_status, "count_turn_ends", _counting_turn_ends)

    def seed_bound(
        self,
        prompts: tuple[str, ...] = ("before",),
        *,
        created_ms: int = 2_000,
        model: str | None = None,
        mirror_existing: bool = False,
    ) -> Path:
        """Seed the bound chat and pre-seed the forwarder cursor onto it.

        With *mirror_existing* the cursor starts at 0 so the existing rows are
        mirrored (used to hold ``retrying_items``); otherwise it starts at the
        store's current rowid so only post-rotation traffic shows up.
        """
        store = _seed_rotation_chat(
            self.chats_root, self.workspace, _CHAT_ID, created_ms, prompts, model=model
        )
        fwd._write_state(
            self.bridge_dir,
            fwd._ForwardState(
                store_path=str(store),
                last_rowid=0 if mirror_existing else fwd._get_current_rowid(store),
                launch_epoch_ms=self.launch_epoch_ms,
            ),
        )
        self.bound_store = store
        return store

    def seed_rotated(
        self,
        prompts: tuple[str, ...] = ("after clear",),
        *,
        created_ms: int = 3_000,
        model: str | None = None,
    ) -> Path:
        """Seed the sibling chat a ``/clear`` would have created."""
        self.rotated_store = _seed_rotation_chat(
            self.chats_root, self.workspace, _CHAT_ID_2, created_ms, prompts, model=model
        )
        return self.rotated_store

    def claim_by_sibling(self, store: Path, *, launch_epoch_ms: int = 500) -> None:
        """Let another live bridge dir claim *store* (earlier launch wins)."""
        sibling = self.bridge_root / "sibling"
        sibling.mkdir(parents=True)
        fwd._write_state(
            sibling,
            fwd._ForwardState(
                store_path=str(store), last_rowid=0, launch_epoch_ms=launch_epoch_ms
            ),
        )

    def active_session_id(self) -> str | None:
        return self.bridge.read_active_session_id(self.bridge_dir)

    async def _wait(self, predicate, max_ticks: int) -> None:
        for _ in range(max_ticks):
            if predicate():
                return
            await asyncio.sleep(0.001)
        raise AssertionError("forwarder never reached the expected state (wedged?)")

    async def run(self, *, until, extra_polls: int = 0, max_ticks: int = 6000) -> None:
        """Drive the loop until *until* holds, then let *extra_polls* more run."""
        task = asyncio.create_task(
            fwd.forward_cursor_store_to_session(
                base_url="http://ap",
                headers={},
                session_id=self.server.old,
                bridge_dir=self.bridge_dir,
                agent_name="cursor-native-ui",
                workspace=self.workspace,
                launch_epoch_ms=self.launch_epoch_ms,
                poll_interval_s=0.001,
                on_session_rotated=lambda old, new: self.rotations.append((old, new)),
            )
        )
        try:
            await self._wait(until, max_ticks)
            if extra_polls:
                target = self.polls + extra_polls
                await self._wait(lambda: self.polls >= target, max_ticks)
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


class TestClearRotation:
    """The pane's ``/clear`` moves the Omnigent session to a new conversation.

    Drives the real poll loop so the whole transaction is covered: detect the
    sibling chat, create the replacement session, carry the runner binding and
    launch settings over, point it at the new chat id, transfer the tmux
    terminal, re-key the superseded session, notify it, and re-aim every
    subsequent POST at the new conversation.
    """

    @pytest.mark.asyncio
    async def test_new_chat_rotates_session_and_transfers_terminal(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        server = _APServer(
            snapshot_extra={
                "workspace": "/ws",
                "terminal_launch_args": ["--force"],
                "model_override": "composer-2.5",
            }
        )
        harness = _RotationHarness(tmp_path, monkeypatch, server)
        harness.seed_bound(("before",))
        harness.seed_rotated(("after clear",))

        await harness.run(until=lambda: ("POST", "/v1/sessions/conv_new/events") in server.paths)

        assert server.unexpected == []
        # The handshake runs in a fixed order: read the old session, create the
        # replacement, bind the runner, point it at the new chat, move the
        # terminal, then release the old session.
        start = server.paths.index(("GET", "/v1/sessions/conv_old"))
        assert server.paths[start : start + 6] == [
            ("GET", "/v1/sessions/conv_old"),
            ("POST", "/v1/sessions"),
            ("PATCH", "/v1/sessions/conv_new"),
            ("PATCH", "/v1/sessions/conv_new"),
            (
                "POST",
                f"/v1/sessions/conv_old/resources/terminals/{_TERMINAL_ID}/transfer",
            ),
            ("PATCH", "/v1/sessions/conv_old"),
        ]
        # The replacement inherits the agent, the labels (including the bridge-id
        # label that keeps it pointed at the ORIGINAL pane's bridge dir) and the
        # launch settings a later cold resume reads back off the snapshot.
        assert server.bodies("POST", "/v1/sessions") == [
            {
                "agent_id": "ag_cursor",
                "labels": {
                    "omnigent.ui": "terminal",
                    "omnigent.cursor_native.bridge_id": "conv_old",
                },
                "workspace": "/ws",
                "terminal_launch_args": ["--force"],
                "model_override": "composer-2.5",
            }
        ]
        assert server.bodies("PATCH", "/v1/sessions/conv_new") == [
            {"runner_id": "runner_one"},
            {"external_session_id": _CHAT_ID_2},
        ]
        assert server.bodies(
            "POST", f"/v1/sessions/conv_old/resources/terminals/{_TERMINAL_ID}/transfer"
        ) == [{"target_session_id": "conv_new"}]
        # The superseded session is re-keyed onto its own bridge id so resuming it
        # later cannot stomp the live pane's ``active_session_id``.
        assert server.bodies("PATCH", "/v1/sessions/conv_old")[-2:] == [
            {"labels": {"omnigent.cursor_native.bridge_id": "conv_old-cleared"}},
            {"runner_id": ""},
        ]
        # The bridge now names the new conversation, so the shared serve-mcp
        # bridge and the other two mirrors follow without being told.
        assert harness.active_session_id() == "conv_new"
        assert harness.rotations == [("conv_old", "conv_new")]
        assert fwd._read_state(harness.bridge_dir).store_path == str(harness.rotated_store)
        # The old conversation only ever receives the supersession trio; the new
        # chat's messages go to the new conversation.
        assert server.event_types("conv_old") == [
            "external_session_status",
            "external_conversation_item",
            "external_session_superseded",
        ]
        assert "external_conversation_item" in server.event_types("conv_new")

    @pytest.mark.asyncio
    async def test_rotation_creates_exactly_one_replacement_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The storm guard: many polls, one replacement conversation.

        The bound store survives ``/clear``, so a detector that kept firing would
        mint a conversation every 0.7s. Once rotated, the new chat becomes the
        bound one and nothing newer exists, so detection goes quiet.
        """
        server = _APServer()
        harness = _RotationHarness(tmp_path, monkeypatch, server)
        harness.seed_bound(("before",))
        harness.seed_rotated(("after clear",))

        await harness.run(until=lambda: harness.active_session_id() == "conv_new", extra_polls=6)

        assert server.create_attempts == 1
        assert harness.rotations == [("conv_old", "conv_new")]

    @pytest.mark.asyncio
    async def test_rotation_gives_up_after_bounded_attempts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A persistently failing handshake is retried a bounded number of times.

        Without the bound, a server that rejects session creation would be hit
        every poll forever. With it the pane's new chat simply stays unmirrored —
        the pre-rotation behaviour — and the bound chat keeps mirroring.
        """
        server = _APServer(create_status=500)
        harness = _RotationHarness(tmp_path, monkeypatch, server)
        harness.seed_bound(("before",))
        harness.seed_rotated(("after clear",))

        await harness.run(
            until=lambda: server.create_attempts >= fwd._MAX_ROTATION_ATTEMPTS,
            extra_polls=6,
        )

        assert server.create_attempts == fwd._MAX_ROTATION_ATTEMPTS
        assert harness.active_session_id() == "conv_old"
        assert harness.rotations == []
        assert fwd._read_state(harness.bridge_dir).store_path == str(harness.bound_store)

    @pytest.mark.asyncio
    async def test_rotation_skipped_while_items_are_retrying(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A retrying item blocks rotation so the old chat's tail is not lost.

        ``/clear`` is usually typed right after a reply, so rotating while the
        last turn is still being retried would drop it.
        """
        server = _APServer(fail_old_item_posts=True)
        harness = _RotationHarness(tmp_path, monkeypatch, server)
        harness.seed_bound(("before",), mirror_existing=True)
        harness.seed_rotated(("after clear",))

        await harness.run(until=lambda: harness.polls >= 8)

        assert server.create_attempts == 0
        assert harness.active_session_id() == "conv_old"

    @pytest.mark.asyncio
    async def test_rotation_skipped_when_chat_claimed_by_other_bridge(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A sibling session in the same cwd owns the new chat — don't take it.

        cursor keeps one chat per working dir, so two cursor-native sessions in
        the same cwd see each other's new chats. The claim check runs against the
        candidate immediately before rotating.
        """
        server = _APServer()
        harness = _RotationHarness(tmp_path, monkeypatch, server)
        harness.seed_bound(("before",))
        rotated = harness.seed_rotated(("after clear",))
        harness.claim_by_sibling(rotated)

        await harness.run(until=lambda: harness.polls >= 8)

        assert server.create_attempts == 0
        assert harness.active_session_id() == "conv_old"

    @pytest.mark.asyncio
    async def test_rotation_resets_rowid_cursor_and_model_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The new conversation starts from row 1 and re-posts the pane's model.

        The replacement conversation's timeline is empty, so the new chat must be
        mirrored from its first row; resetting the model dedupe makes the web
        picker show the right model with no extra code.
        """
        server = _APServer()
        harness = _RotationHarness(tmp_path, monkeypatch, server)
        harness.seed_bound(("before",), model="composer-1")
        harness.seed_rotated(("after clear",), model="composer-2")

        await harness.run(until=lambda: "external_model_change" in server.event_types("conv_new"))

        assert server.unexpected == []
        assert [
            body["data"]["model"]
            for body in server.bodies("POST", "/v1/sessions/conv_new/events")
            if isinstance(body, dict) and body.get("type") == "external_model_change"
        ] == ["composer-2"]
        mirrored = [
            body["data"]["item_data"]["content"][0]["text"]
            for body in server.bodies("POST", "/v1/sessions/conv_new/events")
            if isinstance(body, dict) and body.get("type") == "external_conversation_item"
        ]
        assert "after clear" in mirrored
        state = fwd._read_state(harness.bridge_dir)
        assert state.store_path == str(harness.rotated_store)
        assert state.last_rowid >= 1

    @pytest.mark.asyncio
    async def test_bound_store_is_not_rotated_without_a_new_chat(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No sibling chat, no rotation — the ordinary steady state."""
        server = _APServer()
        harness = _RotationHarness(tmp_path, monkeypatch, server)
        bound = harness.seed_bound(("before",))

        await harness.run(until=lambda: harness.polls >= 8)

        assert server.create_attempts == 0
        assert harness.rotations == []
        assert harness.active_session_id() == "conv_old"
        assert fwd._read_state(harness.bridge_dir).store_path == str(bound)

    @pytest.mark.asyncio
    async def test_attempt_budget_survives_gaps_in_detection(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A poll that detects nothing must not refund the retry budget.

        Detection legitimately drops to ``None`` between attempts — an item goes
        back into retry, the new store is momentarily unreadable. Keying the
        budget on the candidate *chat id* rather than on equality with the last
        observed value is what stops one ``/clear`` from spending five attempts
        over and over and minting a conversation each time.
        """
        server = _APServer(create_status=500)
        harness = _RotationHarness(tmp_path, monkeypatch, server)
        harness.seed_bound(("before",))
        rotated = harness.seed_rotated(("after clear",))

        real_detect = fwd._detect_rotated_chat
        flip = {"n": 0}

        def _intermittent(**kwargs: object) -> Path | None:
            # Every other poll sees nothing, the way a retrying item does.
            flip["n"] += 1
            return None if flip["n"] % 2 == 0 else real_detect(**kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(fwd, "_detect_rotated_chat", _intermittent)

        await harness.run(
            until=lambda: server.create_attempts >= fwd._MAX_ROTATION_ATTEMPTS,
            extra_polls=12,
        )

        assert server.create_attempts == fwd._MAX_ROTATION_ATTEMPTS
        assert harness.active_session_id() == "conv_old"
        assert rotated.exists()

    @pytest.mark.asyncio
    async def test_pending_turn_end_is_posted_to_the_old_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The old chat's completed turn wakes its OWN conversation, not the new one.

        The rotation's ``continue`` skips the turn-end block below it, so a marker
        recorded before the rotation would be posted on the next poll — by which
        time every POST targets the replacement conversation, waking a parent
        orchestrator before the new chat has produced anything.
        """
        from omnigent.harnesses.cursor_native import status as cursor_status

        server = _APServer()
        harness = _RotationHarness(tmp_path, monkeypatch, server)
        harness.seed_bound(("before",))
        harness.seed_rotated(("after clear",))
        cursor_status.record_turn_end(harness.bridge_dir)

        await harness.run(until=lambda: harness.active_session_id() == "conv_new", extra_polls=6)

        assert "external_session_status" in server.event_types("conv_old")
        assert cursor_status.read_posted_count(harness.bridge_dir) == 1
        # The replacement must not inherit the old chat's completion.
        idle_on_new = [
            body
            for body in server.bodies("POST", "/v1/sessions/conv_new/events")
            if isinstance(body, dict) and body.get("type") == "external_session_status"
        ]
        assert idle_on_new == []

    @pytest.mark.asyncio
    async def test_old_session_is_rekeyed_before_its_runner_is_cleared(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two PATCHes, label first — the order that cannot leave two pane owners.

        The server applies a PATCH's fields in separate store calls, so clearing
        the runner and re-keying the bridge label in one request can half-apply.
        Re-keying first means a failure in between leaves the old session pointed
        at its own fresh bridge dir rather than at the live one.
        """
        server = _APServer()
        harness = _RotationHarness(tmp_path, monkeypatch, server)
        harness.seed_bound(("before",))
        harness.seed_rotated(("after clear",))

        await harness.run(until=lambda: harness.active_session_id() == "conv_new", extra_polls=2)

        # Two separate requests, never one combined body, and the label first.
        cleanup = server.bodies("PATCH", "/v1/sessions/conv_old")[-2:]
        assert cleanup == [
            {"labels": {fwd.CURSOR_NATIVE_BRIDGE_ID_LABEL_KEY: "conv_old-cleared"}},
            {"runner_id": ""},
        ]


@pytest.mark.asyncio
async def test_post_clear_supersession_notifies_old_session() -> None:
    """The superseded conversation is told what happened, three ways.

    In order: an ``idle`` status so its spinner stops (its terminal moved away,
    so no turn-end edge will ever arrive), a persisted assistant message linking
    to the new conversation, and a transient redirect event for a live viewer.
    """
    calls: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        calls.append((request.method, request.url.path, body))
        return httpx.Response(200, json={"queued": False, "item_id": "item_x"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        await fwd._post_clear_supersession(
            client,
            old_session_id="conv_old",
            new_session_id="conv_new",
            agent_name="cursor-native-ui",
        )

    assert len(calls) == 3
    assert all(
        (method, path) == ("POST", "/v1/sessions/conv_old/events") for method, path, _ in calls
    )
    assert calls[0][2] == {"type": "external_session_status", "data": {"status": "idle"}}
    notice_body = calls[1][2]
    assert notice_body is not None
    assert notice_body["type"] == "external_conversation_item"
    item_data = notice_body["data"]["item_data"]
    assert item_data["role"] == "assistant"
    assert item_data["agent"] == "cursor-native-ui"
    notice_text = item_data["content"][0]["text"]
    assert "/clear" in notice_text
    assert "/c/conv_new" in notice_text
    assert calls[2][2] == {
        "type": "external_session_superseded",
        "data": {"target_conversation_id": "conv_new"},
    }


@pytest.mark.asyncio
async def test_post_clear_supersession_skips_when_old_equals_new() -> None:
    """Collapsed ids are a no-op: never banner the live conversation."""
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        return httpx.Response(200, json={})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        await fwd._post_clear_supersession(
            client,
            old_session_id="conv_same",
            new_session_id="conv_same",
            agent_name="cursor-native-ui",
        )

    assert calls == []


@pytest.mark.asyncio
async def test_post_clear_supersession_swallows_post_failure() -> None:
    """A failed notice must not break the poll loop — the rotation already ran."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": {"message": "boom"}})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ap"
    ) as client:
        await fwd._post_clear_supersession(
            client,
            old_session_id="conv_old",
            new_session_id="conv_new",
            agent_name="cursor-native-ui",
        )
