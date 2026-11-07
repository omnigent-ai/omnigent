"""Session search on SQLite uses the trigram index only to pick candidate items."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module

import pytest
from sqlalchemy import event, text

from omnigent.db import sqlite_trigram
from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store import sqlalchemy_store
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

migration = import_module(
    "omnigent.db.migrations.versions.ce23c8d4c682_conversation_items_trigram_index"
)
_MATCH = "conversation_items_trigram_fts MATCH"


@pytest.fixture()
def store(conversation_store: SqlAlchemyConversationStore) -> SqlAlchemyConversationStore:
    if conversation_store._conv_engine.dialect.name != "sqlite":
        pytest.skip("the trigram index exists only on SQLite")
    return conversation_store


def _append(store: SqlAlchemyConversationStore, conversation_id: str, *texts: str) -> None:
    store.append(
        conversation_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp",
                data=MessageData(role="user", content=[{"type": "input_text", "text": text}]),
            )
            for text in texts
        ],
    )


def _search(
    store: SqlAlchemyConversationStore, query: str
) -> tuple[dict[str, str | None], list[str]]:
    """Return ``{conversation_id: snippet}`` for *query* and the SQL it ran."""
    statements: list[str] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(store._conv_engine, "before_cursor_execute", capture)
    try:
        page = store.list_conversations(search_query=query, limit=50)
    finally:
        event.remove(store._conv_engine, "before_cursor_execute", capture)
    return {c.id: c.search_snippet for c in page.data}, statements


def _used_index(statements: list[str]) -> bool:
    return any(_MATCH in statement for statement in statements)


def _scan_only(monkeypatch: pytest.MonkeyPatch) -> Callable[[], None]:
    """Make search skip the index until the returned callback restores it."""
    monkeypatch.setattr(sqlalchemy_store, "load_trigram_candidates", lambda session, query: False)
    return monkeypatch.undo


def test_indexed_search_returns_exactly_what_the_scan_returns(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    upper = store.create_conversation(title="upper")
    lower = store.create_conversation(title="lower")
    errors = store.create_conversation(title="errors")
    percent = store.create_conversation(title="percent")
    store.create_conversation(title="outofmemory runbook")
    _append(store, upper.id, "the ÄBC of it")
    _append(store, lower.id, "the äbc of it")
    _append(store, errors.id, "nothing yet", 'OutOfMemoryError FOO-123 "halted"', "OutOfMemory")
    _append(store, percent.id, "progress is 100%", "progress is 100x")
    queries = (
        "äbc",
        "ÄBC",
        "outofmemory",
        "OUTOFMEMORY",
        'FOO-123 "halted"',
        "100%",
        "ss is 1",
        "absent term",
        "of",
    )

    indexed = {query: _search(store, query) for query in queries}
    restore = _scan_only(monkeypatch)
    scanned = {query: _search(store, query)[0] for query in queries}
    restore()

    assert {query: result for query, (result, _) in indexed.items()} == scanned
    # The index folds Unicode case; the search folds ASCII case only, so "ÄBC"
    # never matches, whichever case the query uses.
    assert set(scanned["äbc"]) == set(scanned["ÄBC"]) == {lower.id}
    assert scanned["outofmemory"][errors.id] is not None
    assert "FOO-123" in scanned["outofmemory"][errors.id]
    assert all(
        _used_index(statements) for query, (_, statements) in indexed.items() if query != "of"
    )
    assert not _used_index(indexed["of"][1])


def test_index_entries_of_deleted_items_never_match(store: SqlAlchemyConversationStore) -> None:
    conversation = store.create_conversation(title="general")
    _append(store, conversation.id, "stale needle")
    with store._conv_engine.begin() as connection:
        # Leave the index entry behind, as a missed trigger would.
        connection.exec_driver_sql("DROP TRIGGER conversation_items_trigram_delete")
        connection.exec_driver_sql(
            "DELETE FROM conversation_items WHERE conversation_id = ?",
            (bytes.fromhex(conversation.id),),
        )
        connection.exec_driver_sql(migration._CREATE_TRIGGERS[2])

    result, statements = _search(store, "needle")
    assert _used_index(statements)
    assert result == {}


def test_search_scans_while_a_trigger_is_missing(store: SqlAlchemyConversationStore) -> None:
    conversation = store.create_conversation(title="general")
    with store._conv_engine.begin() as connection:
        connection.exec_driver_sql("DROP TRIGGER conversation_items_trigram_insert")
    _append(store, conversation.id, "unindexed needle")

    result, statements = _search(store, "needle")
    assert not _used_index(statements)
    assert set(result) == {conversation.id}


def test_queries_over_the_candidate_cap_fall_back_to_the_scan(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversations = [store.create_conversation(title=f"c{number}") for number in range(3)]
    for conversation in conversations:
        _append(store, conversation.id, "common words", "more common words")
    loads: list[tuple[bool, int]] = []

    def record_load(session, query):
        used = sqlite_trigram.load_trigram_candidates(session, query)
        loaded = session.execute(
            text("SELECT COUNT(*) FROM temp.conversation_items_trigram_candidates")
        ).scalar_one()
        loads.append((used, loaded))
        return used

    monkeypatch.setattr(sqlalchemy_store, "load_trigram_candidates", record_load)
    monkeypatch.setattr(sqlite_trigram, "MAX_TRIGRAM_CANDIDATES", 5)
    over_cap = _search(store, "common")[0]
    monkeypatch.setattr(sqlite_trigram, "MAX_TRIGRAM_CANDIDATES", 6)
    within_cap = _search(store, "common")[0]

    assert set(over_cap) == {conversation.id for conversation in conversations}
    assert within_cap == over_cap
    # Six items match: one past a cap of five is read, then the search scans.
    assert loads == [(False, 6), (True, 6)]
