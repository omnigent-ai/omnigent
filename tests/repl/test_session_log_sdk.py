"""Unit tests for the SDK-driven and helper paths of ``omnigent.repl._session_log``."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from omnigent_client import StaleCursorError

from omnigent.repl import _session_log
from omnigent.repl._session_log import (
    _build_node_async,
    _build_node_sync,
    _extract_child_conversation_ids,
    _fetch_all_items_sync,
    _fetch_all_items_via_sessions,
    _safe_session_slug,
    collect_log_files,
    default_log_zip_path,
    write_logs_zip,
    write_session_log,
)
from omnigent.stores.conversation_store import ConversationNotFoundError


def _spawn_output(conversation_id: str) -> str:
    return json.dumps({"kind": "sub_agent", "conversation_id": conversation_id})


def _spawn_item(item_id: str, conversation_id: str) -> dict[str, object]:
    return {
        "id": item_id,
        "type": "function_call_output",
        "output": _spawn_output(conversation_id),
    }


class _FakeSessions:
    """Stand-in for ``client.sessions`` backed by an in-memory session tree."""

    def __init__(self, items: dict[str, list[dict[str, object]]]) -> None:
        self.items = items
        self.list_calls: list[tuple[str, str | None]] = []

    async def get(self, session_id: str) -> SimpleNamespace:
        return SimpleNamespace(
            id=session_id,
            title=f"title-{session_id}",
            created_at=1700000000,
            labels={"k": "v"},
        )

    async def list_items(
        self,
        session_id: str,
        *,
        limit: int,
        after: str | None,
        order: str,
    ) -> list[dict[str, object]]:
        assert order == "asc"
        self.list_calls.append((session_id, after))
        rows = self.items.get(session_id, [])
        start = 0
        if after is not None:
            start = next(i for i, row in enumerate(rows) if row["id"] == after) + 1
        return rows[start : start + limit]


def _client(items: dict[str, list[dict[str, object]]]) -> SimpleNamespace:
    return SimpleNamespace(sessions=_FakeSessions(items))


# ── slug + zip path helpers ──────────────────────────────────


@pytest.mark.parametrize("session_id", [None, ""])
def test_safe_session_slug_returns_none_for_missing_id(session_id: str | None) -> None:
    assert _safe_session_slug(session_id) is None


def test_safe_session_slug_replaces_unsafe_characters_and_truncates() -> None:
    assert _safe_session_slug("conv/../x y") == "conv____x_y"
    assert _safe_session_slug("a" * 50) == "a" * 32


def test_default_log_zip_path_includes_session_slug(tmp_path: Path) -> None:
    path = default_log_zip_path(tmp_path, session_id="conv/abc")

    assert path.parent == tmp_path
    assert path.name.startswith("omnigent-logs-conv_abc-")
    assert path.suffix == ".zip"


def test_default_log_zip_path_without_session_uses_module_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_session_log, "DEFAULT_LOG_ZIP_DIR", tmp_path)

    path = default_log_zip_path()

    assert path.parent == tmp_path
    stamp = path.name.removeprefix("omnigent-logs-").removesuffix(".zip")
    # A session-less name is just the timestamp: YYYYMMDD-HHMMSS.
    assert len(stamp) == 15 and stamp[8] == "-"


# ── collect_log_files / write_logs_zip ───────────────────────


def test_collect_log_files_skips_missing_directories_zips_and_duplicates(
    tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    real = logs / "cli.log"
    real.write_text("x", encoding="utf-8")
    alias = tmp_path / "alias.log"
    alias.symlink_to(real)
    (logs / "old.zip").write_text("zip", encoding="utf-8")

    entries = collect_log_files([real, real, alias, logs, logs / "old.zip", logs / "missing.log"])

    # The same file reached by a symlink or repeated path is bundled once.
    assert entries == [(real, "logs/cli.log")]


def test_collect_log_files_disambiguates_colliding_archive_names(tmp_path: Path) -> None:
    first = tmp_path / "a" / "logs" / "cli.log"
    second = tmp_path / "b" / "logs" / "cli.log"
    for path in (first, second):
        path.parent.mkdir(parents=True)
        path.write_text(path.parent.parent.name, encoding="utf-8")

    entries = collect_log_files([first, second])

    assert dict(entries) == {first: "logs/cli.log", second: "logs/01-cli.log"}
    assert [name for _, name in entries] == ["logs/01-cli.log", "logs/cli.log"]


def test_write_logs_zip_defaults_to_session_named_bundle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_session_log, "DEFAULT_LOG_ZIP_DIR", tmp_path / "out")
    log = tmp_path / "logs" / "cli.log"
    log.parent.mkdir()
    log.write_text("hello", encoding="utf-8")

    zip_path, count = write_logs_zip(log_paths=[log], session_id="conv_1")

    assert count == 1
    assert zip_path.parent == tmp_path / "out"
    assert zip_path.name.startswith("omnigent-logs-conv_1-")
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.read("logs/cli.log") == b"hello"


def test_write_logs_zip_with_no_files_still_creates_empty_bundle(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "empty.zip"

    zip_path, count = write_logs_zip(target, log_paths=[tmp_path / "missing.log"])

    assert (zip_path, count) == (target, 0)
    with zipfile.ZipFile(target) as zf:
        assert zf.namelist() == []


def test_write_logs_zip_never_bundles_its_own_output(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    keep = logs / "cli.log"
    keep.write_text("keep", encoding="utf-8")
    # A non-.zip target that already exists passes the ``.zip`` filter in
    # collect_log_files, so only the explicit self-check keeps it out.
    target = logs / "bundle.dat"
    target.write_text("stale bundle", encoding="utf-8")

    zip_path, count = write_logs_zip(target, log_paths=[target, keep])

    assert count == 1
    with zipfile.ZipFile(zip_path) as zf:
        assert zf.namelist() == ["logs/cli.log"]


# ── child discovery ──────────────────────────────────────────


def test_extract_child_ids_reads_flat_and_entity_shapes_and_dedupes() -> None:
    items: list[dict[str, object]] = [
        {"type": "message", "output": _spawn_output("conv_ignored")},
        _spawn_item("i1", "conv_child_a"),
        # Entity shape: output nested under ``data``.
        {"type": "function_call_output", "data": {"output": _spawn_output("conv_child_b")}},
        # Continuation of the same child collapses to one entry.
        _spawn_item("i3", "conv_child_a"),
        # Not a sub-agent handle / non-string output / missing conversation id.
        {"type": "function_call_output", "output": "plain text"},
        {"type": "function_call_output", "output": 7, "data": "not-a-dict"},
        {"type": "function_call_output", "data": {"output": 7}},
        {"type": "function_call_output", "output": json.dumps({"kind": "sub_agent"})},
    ]

    assert _extract_child_conversation_ids(items) == ["conv_child_a", "conv_child_b"]


# ── async SDK dump ───────────────────────────────────────────


async def test_write_session_log_dumps_tree_with_children(tmp_path: Path) -> None:
    client = _client(
        {
            "conv_root": [
                {"id": "i1", "type": "message"},
                _spawn_item("i2", "conv_child"),
                # A handle pointing back at the root must not recurse.
                _spawn_item("i3", "conv_root"),
            ],
            "conv_child": [{"id": "c1", "type": "message"}],
        }
    )

    path = await write_session_log(
        client,  # type: ignore[arg-type]
        "conv_root",
        agent_name="agent_x",
        log_dir=tmp_path / "logs",
    )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["format"] == "omnigent-conversation"
    assert payload["version"] == 1
    assert payload["agent_name"] == "agent_x"
    root = payload["conversation"]
    assert (root["id"], root["title"], root["labels"]) == (
        "conv_root",
        "title-conv_root",
        {"k": "v"},
    )
    assert [item["id"] for item in root["items"]] == ["i1", "i2", "i3"]
    assert [child["id"] for child in root["children"]] == ["conv_child"]
    assert root["children"][0]["items"] == [{"id": "c1", "type": "message"}]


async def test_build_node_async_emits_stub_for_visited_session() -> None:
    client = _client({})

    node = await _build_node_async(client, "conv_a", {"conv_a"})  # type: ignore[arg-type]

    assert node == {"id": "conv_a", "cycle": True, "items": [], "children": []}
    assert client.sessions.list_calls == []


async def test_fetch_items_via_sessions_pages_until_short_page() -> None:
    rows = [{"id": f"i{n}", "type": "message"} for n in range(150)]
    client = _client({"conv_a": rows})

    collected = await _fetch_all_items_via_sessions(client, "conv_a")  # type: ignore[arg-type]

    assert collected == rows
    assert client.sessions.list_calls == [("conv_a", None), ("conv_a", "i99")]


async def test_fetch_items_via_sessions_stops_on_empty_page_or_missing_id() -> None:
    client = _client({"conv_empty": [], "conv_no_id": [{"type": "message"}]})

    assert await _fetch_all_items_via_sessions(client, "conv_empty") == []  # type: ignore[arg-type]
    assert await _fetch_all_items_via_sessions(client, "conv_no_id") == [  # type: ignore[arg-type]
        {"type": "message"}
    ]
    # No cursor could be derived from the id-less row, so no second page is requested.
    assert client.sessions.list_calls == [("conv_empty", None), ("conv_no_id", None)]


async def test_fetch_items_via_sessions_stops_on_none_page() -> None:
    class _NoneSessions:
        async def list_items(self, *args: object, **kwargs: object) -> None:
            return None

    client = SimpleNamespace(sessions=_NoneSessions())

    assert await _fetch_all_items_via_sessions(client, "conv_a") == []  # type: ignore[arg-type]


async def test_fetch_items_via_sessions_restarts_after_stale_cursor() -> None:
    attempts = 0

    class _FlakySessions:
        async def list_items(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
            nonlocal attempts
            attempts += 1
            if attempts < 3:
                raise StaleCursorError("cursor gone", code="stale_cursor")
            return [{"id": "i1"}]

    client = SimpleNamespace(sessions=_FlakySessions())

    assert await _fetch_all_items_via_sessions(client, "conv_a") == [{"id": "i1"}]  # type: ignore[arg-type]
    assert attempts == 3


async def test_fetch_items_via_sessions_propagates_persistent_stale_cursor() -> None:
    attempts = 0

    class _AlwaysStale:
        async def list_items(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
            nonlocal attempts
            attempts += 1
            raise StaleCursorError("cursor gone", code="stale_cursor")

    client = SimpleNamespace(sessions=_AlwaysStale())

    with pytest.raises(StaleCursorError):
        await _fetch_all_items_via_sessions(client, "conv_a")  # type: ignore[arg-type]
    assert attempts == _session_log._STALE_CURSOR_RESTARTS


# ── sync store helpers ───────────────────────────────────────


class _Row:
    def __init__(self, row_id: str) -> None:
        self.id = row_id

    def model_dump(self) -> dict[str, object]:
        return {"id": self.id}


class _FakeStore:
    def __init__(self, pages: list[list[_Row]], conversation: object | None = None) -> None:
        self.pages = pages
        self.conversation = conversation
        self.calls: list[str | None] = []

    def list_items(
        self,
        *,
        conversation_id: str,
        limit: int,
        after: str | None,
        order: str,
    ) -> SimpleNamespace:
        self.calls.append(after)
        return SimpleNamespace(data=self.pages.pop(0) if self.pages else [])

    def get_conversation(self, conversation_id: str) -> object | None:
        return self.conversation


def test_fetch_items_sync_stops_when_last_row_has_no_id() -> None:
    full_page = [_Row(f"i{n}") for n in range(99)] + [_Row("")]
    store = _FakeStore([full_page])

    collected = _fetch_all_items_sync(store, "conv_a")  # type: ignore[arg-type]

    assert len(collected) == 100
    # A full page with no cursor to continue from must not loop forever.
    assert store.calls == [None]


def test_fetch_items_sync_follows_cursor_across_full_pages() -> None:
    store = _FakeStore([[_Row(f"i{n}") for n in range(100)], [_Row("tail")]])

    collected = _fetch_all_items_sync(store, "conv_a")  # type: ignore[arg-type]

    assert len(collected) == 101
    assert store.calls == [None, "i99"]


def test_build_node_sync_emits_stub_for_visited_conversation() -> None:
    node = _build_node_sync(_FakeStore([]), "conv_a", {"conv_a"})  # type: ignore[arg-type]

    assert node == {"id": "conv_a", "cycle": True, "items": [], "children": []}


def test_build_node_sync_raises_for_missing_conversation() -> None:
    with pytest.raises(ConversationNotFoundError, match="conv_gone"):
        _build_node_sync(_FakeStore([]), "conv_gone", set())  # type: ignore[arg-type]


def test_build_node_sync_tolerates_conversation_without_labels() -> None:
    conversation = SimpleNamespace(id="conv_a", title="t", created_at=1)
    store = _FakeStore([[]], conversation)

    node = _build_node_sync(store, "conv_a", set())  # type: ignore[arg-type]

    assert node["labels"] == {}
    assert node["children"] == []
