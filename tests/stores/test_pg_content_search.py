"""Opt-in PostgreSQL trigram fast path for session content search: mode parsing and
eligibility run everywhere; SQL-shape, fallback and parity tests are PostgreSQL-only.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import event

from omnigent.db.utils import get_or_create_engine
from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store import pg_content_search
from omnigent.stores.conversation_store.pg_content_search import (
    CONTENT_SEARCH_MODE_ENV,
    build_content_search_index,
    content_search_mode,
    drop_content_search_index,
    is_trigram_eligible,
    reset_content_search_index_cache,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def test_mode_defaults_to_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(CONTENT_SEARCH_MODE_ENV, raising=False)
    assert content_search_mode() == "off"
    monkeypatch.setenv(CONTENT_SEARCH_MODE_ENV, "  ")
    assert content_search_mode() == "off"


def test_mode_accepts_auto_case_insensitively(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONTENT_SEARCH_MODE_ENV, " Auto ")
    assert content_search_mode() == "auto"
    monkeypatch.setenv(CONTENT_SEARCH_MODE_ENV, "OFF")
    assert content_search_mode() == "off"


def test_mode_rejects_unknown_values(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(CONTENT_SEARCH_MODE_ENV, "on")
    with pytest.raises(RuntimeError, match="OMNIGENT_PG_CONTENT_SEARCH"):
        content_search_mode()


def test_invalid_mode_fails_store_construction(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A typo in the setting surfaces at startup, not on the first search."""
    monkeypatch.setenv(CONTENT_SEARCH_MODE_ENV, "yes")
    with pytest.raises(RuntimeError, match="must be 'auto' or 'off'"):
        SqlAlchemyConversationStore(db_uri)


@pytest.mark.parametrize(
    ("query", "eligible"),
    [
        ("deployment", True),
        ("abc", True),
        ("x-10406", True),
        ("ab", False),
        ("a-b-c", False),
        ("  ", False),
        ("%_%", False),
    ],
)
def test_trigram_eligibility_needs_three_alphanumerics(query: str, eligible: bool) -> None:
    assert is_trigram_eligible(query) is eligible


# --- PostgreSQL-only behaviour ------------------------------------------------


def _captured_sql(store: SqlAlchemyConversationStore, run: Callable[[], object]) -> list[str]:
    statements: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany):  # type: ignore[no-untyped-def]
        statements.append(statement)

    event.listen(store._conv_engine, "before_cursor_execute", _before)
    try:
        run()
    finally:
        event.remove(store._conv_engine, "before_cursor_execute", _before)
    return statements


def _item_sql(statements: list[str]) -> str:
    return " ".join(s for s in statements if "conversation_items" in s).lower()


def _uses_legacy_scan(statements: list[str]) -> bool:
    """Some statement carries the correlated ``EXISTS`` content predicate."""
    return any(
        "exists" in s.lower()
        and "conversation_items.conversation_id = conversations.id" in s.lower()
        for s in statements
    )


def _uses_probe(statements: list[str]) -> bool:
    """Some statement is the capped ``(conversation_id, position)`` index probe."""
    return any(
        "conversation_items.position" in s.lower()
        and "limit" in s.lower()
        and "ilike" in s.lower()
        and "exists" not in s.lower()
        for s in statements
    )


def _message(text: str, response_id: str) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


@pytest.fixture
def pg_uri(db_uri: str) -> str:
    if not db_uri.startswith("postgresql"):
        pytest.skip("the trigram fast path is PostgreSQL-only")
    return db_uri


@pytest.fixture
def indexed_auto_store(
    pg_uri: str, monkeypatch: pytest.MonkeyPatch
) -> SqlAlchemyConversationStore:
    """A store in ``auto`` mode with the trigram index built; the index is dropped afterwards."""
    monkeypatch.setenv(CONTENT_SEARCH_MODE_ENV, "auto")
    engine = get_or_create_engine(pg_uri)
    build_content_search_index(engine)
    reset_content_search_index_cache()
    return SqlAlchemyConversationStore(pg_uri)


@pytest.fixture(autouse=True)
def _drop_index_after(db_uri: str):  # type: ignore[no-untyped-def]
    yield
    if db_uri.startswith("postgresql"):
        drop_content_search_index(get_or_create_engine(db_uri))
    reset_content_search_index_cache()


def _seed(store: SqlAlchemyConversationStore) -> tuple[str, str, str]:
    """Three sessions: a rare content match, a title match, and no match."""
    rare = store.create_conversation()
    store.update_conversation(rare.id, title="notes")
    store.append(
        rare.id,
        [
            _message("hello world", "resp_r1"),
            _message("the ZYGOMORPHIC flower", "resp_r2"),
            _message("zygomorphic again", "resp_r3"),
        ],
    )
    titled = store.create_conversation()
    store.update_conversation(titled.id, title="zygomorphic plan")
    store.append(titled.id, [_message("nothing relevant", "resp_t1")])
    other = store.create_conversation()
    store.update_conversation(other.id, title="unrelated")
    store.append(other.id, [_message("still nothing", "resp_o1")])
    return rare.id, titled.id, other.id


def test_auto_with_index_filters_on_probed_ids_and_keeps_results(
    indexed_auto_store: SqlAlchemyConversationStore,
) -> None:
    """A rare term is answered from the index probe with the legacy result set."""
    rare_id, titled_id, other_id = _seed(indexed_auto_store)

    statements = _captured_sql(
        indexed_auto_store,
        lambda: indexed_auto_store.list_conversations(search_query="zygomorphic"),
    )
    assert _uses_probe(statements), statements
    assert not _uses_legacy_scan(statements), statements
    assert "ilike" in _item_sql(statements), statements

    page = indexed_auto_store.list_conversations(search_query="zygomorphic")
    ids = {c.id for c in page.data}
    assert ids == {rare_id, titled_id}
    assert other_id not in ids
    by_id = {c.id: c for c in page.data}
    # The snippet comes from the earliest matching item, without a further ILIKE.
    assert by_id[rare_id].search_snippet is not None
    assert "ZYGOMORPHIC" in by_id[rare_id].search_snippet
    assert by_id[titled_id].search_snippet is None


def test_auto_with_index_matches_legacy_results_and_snippets(
    indexed_auto_store: SqlAlchemyConversationStore, pg_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both paths return the same sessions, order and snippets."""
    _seed(indexed_auto_store)
    fast = indexed_auto_store.list_conversations(search_query="zygomorphic")

    monkeypatch.setenv(CONTENT_SEARCH_MODE_ENV, "off")
    legacy_store = SqlAlchemyConversationStore(pg_uri)
    legacy = legacy_store.list_conversations(search_query="zygomorphic")

    assert [c.id for c in fast.data] == [c.id for c in legacy.data]
    assert [c.search_snippet for c in fast.data] == [c.search_snippet for c in legacy.data]


def test_absent_term_returns_nothing_without_scanning(
    indexed_auto_store: SqlAlchemyConversationStore,
) -> None:
    _seed(indexed_auto_store)
    statements = _captured_sql(
        indexed_auto_store,
        lambda: indexed_auto_store.list_conversations(search_query="zzqx-absent-term"),
    )
    assert _uses_probe(statements), statements
    assert not _uses_legacy_scan(statements), statements
    assert indexed_auto_store.list_conversations(search_query="zzqx-absent-term").data == []


def test_overflowing_probe_falls_back_to_legacy_scan(
    indexed_auto_store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """More matching items than the cap means a common term: use the legacy query."""
    rare_id, titled_id, _other = _seed(indexed_auto_store)
    monkeypatch.setattr(pg_content_search, "CONTENT_SEARCH_PROBE_CAP", 1)

    statements = _captured_sql(
        indexed_auto_store,
        lambda: indexed_auto_store.list_conversations(search_query="zygomorphic"),
    )
    assert _uses_probe(statements), statements
    assert _uses_legacy_scan(statements), statements
    page = indexed_auto_store.list_conversations(search_query="zygomorphic")
    assert {c.id for c in page.data} == {rare_id, titled_id}
    assert next(c for c in page.data if c.id == rare_id).search_snippet is not None


def test_probe_respects_permission_scope(
    indexed_auto_store: SqlAlchemyConversationStore, pg_uri: str
) -> None:
    """Probed ids are still filtered by the caller's ACL."""
    from omnigent.server.auth import LEVEL_OWNER
    from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore

    rare_id, titled_id, _other = _seed(indexed_auto_store)
    perms = SqlAlchemyPermissionStore(pg_uri)
    perms.ensure_user("alice@example.com")
    perms.grant("alice@example.com", titled_id, LEVEL_OWNER)

    statements = _captured_sql(
        indexed_auto_store,
        lambda: indexed_auto_store.list_conversations(
            search_query="zygomorphic", accessible_by="alice@example.com"
        ),
    )
    assert _uses_probe(statements), statements
    page = indexed_auto_store.list_conversations(
        search_query="zygomorphic", accessible_by="alice@example.com"
    )
    assert {c.id for c in page.data} == {titled_id}
    assert rare_id not in {c.id for c in page.data}


def test_off_keeps_legacy_scan_even_with_index(
    pg_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONTENT_SEARCH_MODE_ENV, "off")
    build_content_search_index(get_or_create_engine(pg_uri))
    store = SqlAlchemyConversationStore(pg_uri)
    _seed(store)

    statements = _captured_sql(store, lambda: store.list_conversations(search_query="zygomorphic"))
    assert _uses_legacy_scan(statements), statements
    assert not _uses_probe(statements), statements


def test_auto_without_index_keeps_legacy_scan(
    pg_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(CONTENT_SEARCH_MODE_ENV, "auto")
    drop_content_search_index(get_or_create_engine(pg_uri))
    store = SqlAlchemyConversationStore(pg_uri)
    _seed(store)

    statements = _captured_sql(store, lambda: store.list_conversations(search_query="zygomorphic"))
    assert _uses_legacy_scan(statements), statements
    assert not _uses_probe(statements), statements


def test_auto_picks_up_an_index_built_later(pg_uri: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalog check is cached but refreshed, so a later build is used."""
    monkeypatch.setenv(CONTENT_SEARCH_MODE_ENV, "auto")
    drop_content_search_index(get_or_create_engine(pg_uri))
    store = SqlAlchemyConversationStore(pg_uri)
    _seed(store)
    assert _uses_legacy_scan(
        _captured_sql(store, lambda: store.list_conversations(search_query="zygomorphic"))
    )

    build_content_search_index(get_or_create_engine(pg_uri))
    assert _uses_probe(
        _captured_sql(store, lambda: store.list_conversations(search_query="zygomorphic"))
    )


def test_short_query_keeps_legacy_scan(indexed_auto_store: SqlAlchemyConversationStore) -> None:
    """Fewer than three alphanumerics yields no trigram, so the index cannot help."""
    _seed(indexed_auto_store)
    statements = _captured_sql(
        indexed_auto_store, lambda: indexed_auto_store.list_conversations(search_query="zy")
    )
    assert _uses_legacy_scan(statements), statements
    assert not _uses_probe(statements), statements


def test_build_index_is_idempotent_and_valid(pg_uri: str) -> None:
    engine = get_or_create_engine(pg_uri)
    assert build_content_search_index(engine) is True
    assert build_content_search_index(engine) is False
    with engine.connect() as conn:
        from sqlalchemy import text

        definition = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :n"),
            {"n": pg_content_search.CONTENT_SEARCH_INDEX},
        ).scalar()
    assert definition is not None and "gin_trgm_ops" in definition
    assert drop_content_search_index(engine) is True
    assert drop_content_search_index(engine) is False
