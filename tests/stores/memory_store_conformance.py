"""Behavior every :class:`MemoryStore` backend must share.

Subclass :class:`MemoryStoreConformance` and provide a ``store`` fixture; a
backend that can search subclasses :class:`SearchableMemoryStoreConformance`.
A workspace scope is applied by the ``workspace`` fixture so backends that key on
the ambient workspace can be exercised across tenants.
"""

from __future__ import annotations

import random
import string

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.memory_store import (
    MAX_CONTENT_BYTES,
    MAX_PAGE_SIZE,
    MAX_SEARCH_LIMIT,
    MemoryStore,
)

ALICE = "user:alice"
BOB = "user:bob"


class MemoryStoreConformance:
    """Mixin of backend-independent tests; needs a ``store`` fixture."""

    def test_write_then_get_round_trips(self, store: MemoryStore) -> None:
        written = store.write(ALICE, "/user/preferences/style", "Prefers tabs", "Code style")

        loaded = store.get(ALICE, "/user/preferences/style")

        assert loaded is not None
        assert (loaded.actor_id, loaded.path) == (ALICE, "/user/preferences/style")
        assert loaded.content == "Prefers tabs"
        assert loaded.description == "Code style"
        assert loaded.created_at == written.created_at

    def test_write_replaces_the_entry_at_a_path(self, store: MemoryStore) -> None:
        first = store.write(ALICE, "/a", "old", "old summary")

        second = store.write(ALICE, "/a", "new")

        assert second.created_at == first.created_at
        loaded = store.get(ALICE, "/a")
        assert loaded is not None
        assert loaded.content == "new"
        assert loaded.description is None

    def test_get_missing_returns_none(self, store: MemoryStore) -> None:
        assert store.get(ALICE, "/nope") is None

    def test_delete_reports_whether_an_entry_existed(self, store: MemoryStore) -> None:
        store.write(ALICE, "/a", "x")

        assert store.delete(ALICE, "/a") is True
        assert store.delete(ALICE, "/a") is False
        assert store.get(ALICE, "/a") is None

    def test_actors_are_isolated(self, store: MemoryStore) -> None:
        store.write(ALICE, "/shared/path", "alice secret")

        assert store.get(BOB, "/shared/path") is None
        assert store.list(BOB).entries == []
        assert store.delete(BOB, "/shared/path") is False
        loaded = store.get(ALICE, "/shared/path")
        assert loaded is not None
        assert loaded.content == "alice secret"

    def test_workspaces_are_isolated(self, store: MemoryStore) -> None:
        with workspace_scope(1):
            store.write(ALICE, "/p", "workspace one")
        with workspace_scope(2):
            assert store.get(ALICE, "/p") is None
            assert store.list(ALICE).entries == []
            store.write(ALICE, "/p", "workspace two")
        with workspace_scope(1):
            loaded = store.get(ALICE, "/p")
            assert loaded is not None
            assert loaded.content == "workspace one"

    def test_list_returns_paths_and_descriptions_without_content(self, store: MemoryStore) -> None:
        store.write(ALICE, "/b", "second", "B")
        store.write(ALICE, "/a", "first", "A")

        page = store.list(ALICE)

        assert sorted((e.path, e.description) for e in page.entries) == [("/a", "A"), ("/b", "B")]
        assert all(e.content == "" for e in page.entries)
        assert page.next_page_token is None

    def test_list_prefix_matches_whole_segments(self, store: MemoryStore) -> None:
        for path in ("/projects", "/projects/api", "/projects/api/notes", "/projects-old/x"):
            store.write(ALICE, path, "x")

        paths = _all_paths(store, path_prefix="/projects")

        assert sorted(paths) == ["/projects", "/projects/api", "/projects/api/notes"]

    @pytest.mark.parametrize("prefix", [None, "/p"])
    def test_list_pages_through_every_entry_once(
        self, store: MemoryStore, prefix: str | None
    ) -> None:
        expected = ["/p", *(f"/p/e{i:02d}" for i in range(5))]
        for path in [*expected, "/p-sibling", "/q"]:
            store.write(ALICE, path, "x")
        if prefix is None:
            expected += ["/p-sibling", "/q"]

        seen = _all_paths(store, path_prefix=prefix, page_size=2)

        assert sorted(seen) == sorted(expected)
        assert len(seen) == len(expected)

    def test_list_page_size_is_bounded(self, store: MemoryStore) -> None:
        for page_size in (0, MAX_PAGE_SIZE + 1):
            with pytest.raises(OmnigentError) as excinfo:
                store.list(ALICE, page_size=page_size)
            assert excinfo.value.code == ErrorCode.INVALID_INPUT

    @pytest.mark.parametrize("actor", ["", "   "])
    def test_missing_actor_fails_closed_on_every_operation(
        self, store: MemoryStore, actor: str
    ) -> None:
        calls = [
            lambda: store.write(actor, "/a", "x"),
            lambda: store.get(actor, "/a"),
            lambda: store.list(actor),
            lambda: store.delete(actor, "/a"),
        ]
        for call in calls:
            with pytest.raises(OmnigentError) as excinfo:
                call()
            assert excinfo.value.code == ErrorCode.INVALID_INPUT

    @pytest.mark.parametrize(
        "path",
        [
            "",
            "/",
            "relative",
            "/a//b",
            "/a/./b",
            "/a/../b",
            "/" + "x" * 600,
            "/a ",
            "/ a",
            "/a /b",
            "/a\tb",
            "/a\nb",
            "/a\x00b",
            "/a\x7fb",
            '/a"b',
            "/a`b`",
            "/a:b",
            "/a#b",
            "/a[b]",
        ],
    )
    def test_malformed_paths_are_rejected(self, store: MemoryStore, path: str) -> None:
        with pytest.raises(OmnigentError) as excinfo:
            store.write(ALICE, path, "x")
        assert excinfo.value.code == ErrorCode.INVALID_INPUT

    @pytest.mark.parametrize("path", ["/a b/c", "/notes/2026.11-review_v2", "/p/é", "/中文/笔记"])
    def test_plain_names_with_spaces_and_any_letters_are_accepted(
        self, store: MemoryStore, path: str
    ) -> None:
        store.write(ALICE, path, "x")

        loaded = store.get(ALICE, path)
        assert loaded is not None
        assert loaded.path == path

    def test_empty_and_oversized_content_are_rejected(self, store: MemoryStore) -> None:
        # "é" is two UTF-8 bytes: the limit counts bytes, not characters.
        multibyte = "é" * (MAX_CONTENT_BYTES // 2 + 1)
        for content in ("", "   ", "x" * (MAX_CONTENT_BYTES + 1), multibyte):
            with pytest.raises(OmnigentError) as excinfo:
                store.write(ALICE, "/a", content)
            assert excinfo.value.code == ErrorCode.INVALID_INPUT

    def test_content_at_the_limit_round_trips(self, store: MemoryStore) -> None:
        content = largest_hard_to_compress_content()

        store.write(ALICE, "/big", content)

        loaded = store.get(ALICE, "/big")
        assert loaded is not None
        assert loaded.content == content


class SearchableMemoryStoreConformance(MemoryStoreConformance):
    """The contract plus :meth:`MemoryStore.search`, for backends that offer it."""

    def test_declares_search(self, store: MemoryStore) -> None:
        assert store.supports_search is True

    def test_search_is_case_insensitive_and_honours_prefix_and_limit(
        self, store: MemoryStore
    ) -> None:
        store.write(ALICE, "/projects/api/a", "Retries use exponential BACKOFF")
        store.write(ALICE, "/projects/web/b", "backoff in the web client")
        store.write(ALICE, "/user/c", "backoff preference")

        scoped = store.search(ALICE, "backoff", path_prefix="/projects")
        limited = store.search(ALICE, "backoff", limit=1)

        assert {h.entry.path for h in scoped} == {"/projects/api/a", "/projects/web/b"}
        assert len(limited) == 1

    def test_search_without_match_is_empty(self, store: MemoryStore) -> None:
        store.write(ALICE, "/a", "hello")

        assert store.search(ALICE, "zebra") == []

    def test_search_covers_only_the_actors_memory(self, store: MemoryStore) -> None:
        store.write(ALICE, "/shared/path", "alice secret")

        assert store.search(BOB, "secret") == []

    @pytest.mark.parametrize("actor", ["", "   "])
    def test_search_without_an_actor_fails_closed(self, store: MemoryStore, actor: str) -> None:
        with pytest.raises(OmnigentError) as excinfo:
            store.search(actor, "x")
        assert excinfo.value.code == ErrorCode.INVALID_INPUT

    def test_search_limit_is_bounded(self, store: MemoryStore) -> None:
        for limit in (0, MAX_SEARCH_LIMIT + 1):
            with pytest.raises(OmnigentError) as excinfo:
                store.search(ALICE, "x", limit=limit)
            assert excinfo.value.code == ErrorCode.INVALID_INPUT


def largest_hard_to_compress_content() -> str:
    """
    Exactly :data:`MAX_CONTENT_BYTES` UTF-8 bytes that compress poorly.

    Mixed one-, two- and three-byte characters from a fixed seed, so a backend
    that compresses must still fit the worst realistic case.
    """
    rng = random.Random(1)
    alphabet = string.ascii_letters + string.digits + "éßøЖ中文字"
    chars: list[str] = []
    size = 0
    while True:
        char = rng.choice(alphabet)
        width = len(char.encode())
        if size + width > MAX_CONTENT_BYTES:
            break
        chars.append(char)
        size += width
    chars.append("x" * (MAX_CONTENT_BYTES - size))
    return "".join(chars)


def _all_paths(store: MemoryStore, *, path_prefix: str | None = None, page_size: int = 100):
    """Every path a listing returns, following page tokens to the end."""
    paths: list[str] = []
    token = None
    while True:
        page = store.list(ALICE, path_prefix=path_prefix, page_size=page_size, page_token=token)
        assert len(page.entries) <= page_size
        paths.extend(e.path for e in page.entries)
        token = page.next_page_token
        if token is None:
            return paths
