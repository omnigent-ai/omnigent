"""Tests for :class:`HindsightMemoryStore` against an in-memory Hindsight stand-in."""

from __future__ import annotations

import pytest

from omnigent.db.db_models import workspace_scope
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.stores.memory_store import MemoryStore
from omnigent.stores.memory_store.hindsight_store import (
    HindsightMemoryStore,
    bank_id_for,
    create_hindsight_client,
)
from tests.stores.fake_hindsight import FakeHindsight
from tests.stores.memory_store_conformance import ALICE, BOB, SearchableMemoryStoreConformance


@pytest.fixture(params=[False, True], ids=["dict-list-items", "model-list-items"])
def fake(request: pytest.FixtureRequest) -> FakeHindsight:
    """Both document-list shapes: dicts (locked 0.8.x) and models (1.x)."""
    return FakeHindsight(list_items_as_models=request.param)


@pytest.fixture()
def store(fake: FakeHindsight) -> MemoryStore:
    return HindsightMemoryStore(fake)


class TestHindsightMemoryStore(SearchableMemoryStoreConformance):
    """The shared backend contract against Hindsight."""

    def test_each_path_is_one_replaced_document_in_the_actors_bank(
        self, store: MemoryStore, fake: FakeHindsight
    ) -> None:
        store.write(ALICE, "/projects/api/notes", "first", "Notes")
        store.write(ALICE, "/projects/api/notes", "second")

        bank = bank_id_for(0, ALICE)
        assert list(fake.banks) == [bank]
        assert list(fake.banks[bank]) == ["%2Fprojects%2Fapi%2Fnotes"]
        assert {r["update_mode"] for r in fake.retains} == {"replace"}
        assert fake.banks[bank]["%2Fprojects%2Fapi%2Fnotes"].original_text == "second"

    def test_banks_differ_by_workspace_and_by_exact_actor(self) -> None:
        assert bank_id_for(1, ALICE) != bank_id_for(2, ALICE)
        assert bank_id_for(0, ALICE) != bank_id_for(0, BOB)
        # Ids that sanitize to the same readable text still get distinct banks.
        assert bank_id_for(0, "user:a b") != bank_id_for(0, "user:a_b")

    def test_a_bank_is_created_once_and_a_creation_failure_is_tolerated(self) -> None:
        fake = FakeHindsight(fail_create_bank=True)
        store = HindsightMemoryStore(fake)

        store.write(ALICE, "/a", "x")
        store.write(ALICE, "/b", "y")

        assert [c for c in fake.calls if c[0] == "create_bank"] == [
            ("create_bank", bank_id_for(0, ALICE))
        ]
        assert store.get(ALICE, "/b") is not None

    def test_workspace_scope_selects_the_bank(
        self, store: MemoryStore, fake: FakeHindsight
    ) -> None:
        with workspace_scope(7):
            store.write(ALICE, "/a", "x")

        assert list(fake.banks) == [bank_id_for(7, ALICE)]

    def test_listing_reaches_every_document_beyond_a_thousand(
        self, store: MemoryStore, fake: FakeHindsight
    ) -> None:
        bank = bank_id_for(0, ALICE)
        for i in range(1_001):
            store.write(ALICE, f"/notes/{i:04d}", "x")
        fake.calls.clear()

        seen: list[str] = []
        token = None
        while True:
            page = store.list(ALICE, page_size=100, page_token=token)
            seen.extend(e.path for e in page.entries)
            token = page.next_page_token
            if token is None:
                break

        assert sorted(seen) == [f"/notes/{i:04d}" for i in range(1_001)]
        # One bounded request per page, never a scan of the whole bank.
        assert [c for c in fake.calls if c[0] == "list_documents"] == [
            ("list_documents", bank, None)
        ] * 11

    def test_a_server_returning_less_than_asked_still_pages_to_the_end(self) -> None:
        fake = FakeHindsight(list_limit_cap=3)
        store = HindsightMemoryStore(fake)
        for i in range(10):
            store.write(ALICE, f"/n/{i}", "x")

        seen: list[str] = []
        token = None
        while True:
            page = store.list(ALICE, page_size=100, page_token=token)
            seen.extend(e.path for e in page.entries)
            token = page.next_page_token
            if token is None:
                break

        assert sorted(seen) == [f"/n/{i}" for i in range(10)]

    @pytest.mark.parametrize("token", ["/a", "-1", "1e3", "9" * 13])
    def test_a_page_token_from_elsewhere_is_rejected(self, store: MemoryStore, token: str) -> None:
        with pytest.raises(OmnigentError) as excinfo:
            store.list(ALICE, page_token=token)
        assert excinfo.value.code == ErrorCode.INVALID_INPUT

    def test_search_maps_documents_back_to_paths_best_first(self, store: MemoryStore) -> None:
        store.write(ALICE, "/a", "deploy once")
        store.write(ALICE, "/b", "deploy deploy pipeline")

        hits = store.search(ALICE, "deploy pipeline")

        assert [h.entry.path for h in hits] == ["/b", "/a"]
        assert hits[0].score > hits[1].score

    def test_backend_failures_become_a_clean_error_without_internals(self) -> None:
        class Broken(FakeHindsight):
            async def aretain(self, *args, **kwargs):
                raise RuntimeError("secret-api-key-123 rejected")

        store = HindsightMemoryStore(Broken())

        with pytest.raises(OmnigentError) as excinfo:
            store.write(ALICE, "/a", "x")

        assert excinfo.value.code == ErrorCode.INTERNAL_ERROR
        assert "secret-api-key-123" not in str(excinfo.value)

    def test_missing_optional_dependency_is_reported(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "hindsight_client":
                raise ImportError(name)
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        with pytest.raises(OmnigentError, match=r"omnigent\[hindsight\]"):
            create_hindsight_client(None, "key")


def test_a_client_without_replace_on_retain_is_refused_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hindsight_client

    class OldHindsight:
        """A pre-0.5 client: retain cannot replace a document."""

        def __init__(self, **_kwargs: object) -> None:
            pass

        async def aretain(self, bank_id: str, content: str, document_id: str | None = None):
            raise AssertionError("never called")

    monkeypatch.setattr(hindsight_client, "Hindsight", OldHindsight)

    with pytest.raises(OmnigentError, match=r"hindsight-client>=0\.5\.0"):
        create_hindsight_client(None, "key")


def test_the_installed_client_is_accepted() -> None:
    client = create_hindsight_client("https://hindsight.example.com", "key")

    assert hasattr(client, "documents")
