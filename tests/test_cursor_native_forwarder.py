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
    await fwd._patch_external_session_id(client, session_id="conv_abc", chat_id="chat-uuid-123")  # type: ignore[arg-type]
    assert len(client.patches) == 1
    url, body = client.patches[0]
    assert url == "/v1/sessions/conv_abc"
    assert body == {"external_session_id": "chat-uuid-123"}


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


class TestForwardLoopExternalSessionId:
    """The poll loop patches external_session_id once when the store is found."""

    @staticmethod
    def _seed(store: Path, text: str = "hello") -> None:
        writer = _make_store(
            store, [("u", _user(f"<user_query>\n{text}\n</user_query>"))]
        )
        writer.close()

    @pytest.mark.asyncio
    async def test_patches_chat_id_on_first_store_discovery(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The forwarder PATCHes external_session_id with the cursor chat_id."""
        store = tmp_path / "chat-uuid-abc123" / "store.db"
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
        assert chat_id == "chat-uuid-abc123"

    @pytest.mark.asyncio
    async def test_patches_only_once_across_multiple_polls(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """external_session_id is patched exactly once, not on every poll tick."""
        store = tmp_path / "chat-uuid-xyz" / "store.db"
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
