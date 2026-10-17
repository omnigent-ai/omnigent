"""Opted-in PostgreSQL content search must probe the trigram index, not scan items. Uses
only long-standing symbols and raw SQL so an unfixed build fails on query shape, not import.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from omnigent.db.utils import get_or_create_engine
from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_INDEX = "ix_conversation_items_search_text_gin_trgm"


@pytest.fixture
def pg_uri(db_uri: str) -> str:
    if not db_uri.startswith("postgresql"):
        pytest.skip("the trigram fast path is PostgreSQL-only")
    return db_uri


@pytest.fixture
def trigram_index(pg_uri: str):  # type: ignore[no-untyped-def]
    engine = get_or_create_engine(pg_uri)
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        conn.execute(
            sa.text(
                f"CREATE INDEX IF NOT EXISTS {_INDEX} ON conversation_items "
                "USING gin (search_text gin_trgm_ops)"
            )
        )
    yield
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(sa.text(f"DROP INDEX IF EXISTS {_INDEX}"))


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


def _message(text: str, response_id: str) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
    )


def _seed(store: SqlAlchemyConversationStore) -> tuple[str, str]:
    matching = store.create_conversation()
    store.update_conversation(matching.id, title="notes")
    store.append(
        matching.id,
        [_message("hello world", "resp_p1"), _message("the ZYGOMORPHIC flower", "resp_p2")],
    )
    other = store.create_conversation()
    store.update_conversation(other.id, title="unrelated")
    store.append(other.id, [_message("still nothing", "resp_p3")])
    return matching.id, other.id


def test_opted_in_search_probes_the_index_instead_of_scanning_items(
    pg_uri: str, trigram_index: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the index and ``auto``, a rare term issues no correlated item scan."""
    monkeypatch.setenv("OMNIGENT_PG_CONTENT_SEARCH", "auto")
    store = SqlAlchemyConversationStore(pg_uri)
    matching_id, other_id = _seed(store)

    statements = _captured_sql(store, lambda: store.list_conversations(search_query="zygomorphic"))
    item_statements = [s.lower() for s in statements if "conversation_items" in s]
    assert item_statements, statements
    assert not any(
        "exists" in s and "conversation_items.conversation_id = conversations.id" in s
        for s in item_statements
    ), item_statements
    assert any(
        "ilike" in s and "conversation_items.position" in s and "limit" in s
        for s in item_statements
    ), item_statements

    page = store.list_conversations(search_query="zygomorphic")
    assert [c.id for c in page.data] == [matching_id]
    assert other_id not in {c.id for c in page.data}
    assert page.data[0].search_snippet is not None
    assert "ZYGOMORPHIC" in page.data[0].search_snippet


def test_opted_in_search_returns_the_same_page_as_the_scan(
    pg_uri: str, trigram_index: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Both paths agree on sessions, order and snippets."""
    monkeypatch.setenv("OMNIGENT_PG_CONTENT_SEARCH", "off")
    scanning = SqlAlchemyConversationStore(pg_uri)
    _seed(scanning)
    expected = scanning.list_conversations(search_query="zygomorphic")

    monkeypatch.setenv("OMNIGENT_PG_CONTENT_SEARCH", "auto")
    probing = SqlAlchemyConversationStore(pg_uri)
    actual = probing.list_conversations(search_query="zygomorphic")

    assert [c.id for c in actual.data] == [c.id for c in expected.data]
    assert [c.search_snippet for c in actual.data] == [c.search_snippet for c in expected.data]
