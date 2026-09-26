from __future__ import annotations

import pytest
from sqlalchemy import event, text

from omnigent.db import sqlite_trigram
from omnigent.db.sqlite_trigram import (
    TRIGRAM_FTS_TABLE,
    initialize_trigram_search,
    supports_trigram_fts,
    trigram_match_expression,
)
from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


@pytest.fixture()
def store(conversation_store: SqlAlchemyConversationStore) -> SqlAlchemyConversationStore:
    if not supports_trigram_fts(conversation_store._conv_engine):
        pytest.skip("SQLite trigram tokenizer is unavailable")
    return conversation_store


def _catch_up(store: SqlAlchemyConversationStore) -> None:
    sqlite_trigram._catch_up(store._conv_engine)
    with store._conv_engine.begin() as connection:
        progress = connection.exec_driver_sql(sqlite_trigram._SELECT_PROGRESS).one()
        assert not sqlite_trigram._has_pending_work(progress), progress
        connection.exec_driver_sql(
            f"INSERT INTO {TRIGRAM_FTS_TABLE}({TRIGRAM_FTS_TABLE}, rank) "
            "VALUES ('integrity-check', 1)"
        )


def _append(store: SqlAlchemyConversationStore, conversation_id: str, *messages: str) -> None:
    store.append(
        conversation_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp",
                data=MessageData(role="user", content=[{"type": "input_text", "text": message}]),
            )
            for message in messages
        ],
    )


def _search_ids(store: SqlAlchemyConversationStore, query: str) -> set[str]:
    return {conversation.id for conversation in store.list_conversations(search_query=query).data}


def _indexed_count(store: SqlAlchemyConversationStore, query: str) -> int:
    with store._conv_engine.connect() as connection:
        return connection.execute(
            text(f"SELECT COUNT(*) FROM {TRIGRAM_FTS_TABLE} WHERE {TRIGRAM_FTS_TABLE} MATCH :q"),
            {"q": trigram_match_expression(query)},
        ).scalar_one()


def test_trigram_match_expression_quotes_literal_runs() -> None:
    assert trigram_match_expression("abc") == '"abc"'
    assert trigram_match_expression('say "hi" now') == '"say ""hi"" now"'
    assert trigram_match_expression("100%") == '"100"'
    assert trigram_match_expression("search_text") == '"search" AND "text"'
    assert trigram_match_expression("ab") is None
    assert trigram_match_expression("ab_cd") is None


def test_trigram_search_matches_like_before_and_after_indexing(
    store: SqlAlchemyConversationStore,
) -> None:
    fragment = store.create_conversation(title="general")
    wildcard = store.create_conversation(title="percent")
    literal = store.create_conversation(title="literal")
    store.create_conversation(title="other")
    _append(store, fragment.id, "nothing", 'OutOfMemoryError FOO-123 "halted"', "OutOfMemory")
    _append(store, wildcard.id, "progress is 100x")
    _append(store, literal.id, "progress is 100%")
    expected = {
        "outofmemory": {fragment.id},
        "OUTOFMEMORY": {fragment.id},
        'FOO-123 "halted"': {fragment.id},
        "100%": {wildcard.id, literal.id},
        "progress_is": {wildcard.id, literal.id},
        "ess is 100x": {wildcard.id},
    }
    statements: list[tuple[str, object]] = []

    def capture(conn, cursor, statement, parameters, context, executemany):
        if f"FROM {TRIGRAM_FTS_TABLE}" in statement:
            statements.append((statement, parameters))

    for _ in range(2):
        statements.clear()
        event.listen(store._conv_engine, "before_cursor_execute", capture)
        try:
            assert {query: _search_ids(store, query) for query in expected} == expected
            [page] = store.list_conversations(search_query="outofmemory").data
        finally:
            event.remove(store._conv_engine, "before_cursor_execute", capture)
        assert len(statements) == len(expected) + 1
        assert page.search_snippet is not None and "FOO-123" in page.search_snippet
        _catch_up(store)

    for sql, parameters in (statements[0], statements[3]):
        with store._conv_engine.connect() as connection:
            plan = connection.exec_driver_sql(f"EXPLAIN QUERY PLAN {sql}", parameters)
            details = [row.detail for row in plan]
        assert any(TRIGRAM_FTS_TABLE in d and "VIRTUAL TABLE INDEX" in d for d in details), details
        assert any("INTEGER PRIMARY KEY (rowid>?)" in d for d in details), details


async def test_item_writes_and_deletes_keep_trigram_index_consistent(
    store: SqlAlchemyConversationStore,
) -> None:
    with store._conv_engine.connect() as connection:
        triggers = connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'trigger'"
        ).scalars()
        assert all(TRIGRAM_FTS_TABLE not in sql for sql in triggers)
    source = store.create_conversation(title="source")
    kept = store.create_conversation(title="kept")
    _append(store, source.id, "bulkneedle one", "bulkneedle two")
    _append(store, kept.id, "bulkneedle three")
    fork = store.fork_conversation(source.id)
    _catch_up(store)
    assert _search_ids(store, "bulkneedle") == {source.id, fork.id, kept.id}
    assert _indexed_count(store, "bulkneedle") == 5

    assert await store.delete_conversation(source.id)
    with store._conv_engine.begin() as connection:
        connection.execute(
            text("DELETE FROM conversation_items WHERE conversation_id = :id"),
            {"id": bytes.fromhex(kept.id)},
        )
    assert _search_ids(store, "bulkneedle") == {fork.id}
    _catch_up(store)
    assert _indexed_count(store, "bulkneedle") == 2


def test_trigram_search_recovers_items_written_without_triggers(
    store: SqlAlchemyConversationStore,
) -> None:
    kept = store.create_conversation(title="kept")
    missed = store.create_conversation(title="missed")
    _append(store, kept.id, "alphaneedle")
    _catch_up(store)
    with store._conv_engine.begin() as connection:
        for trigger in (sqlite_trigram._INSERT_TRIGGER, sqlite_trigram._DELETE_TRIGGER):
            connection.exec_driver_sql(f"DROP TRIGGER {trigger}")
    _append(store, missed.id, "betaneedle")

    initialize_trigram_search(store._conv_engine)
    _catch_up(store)
    assert _search_ids(store, "needle") == {kept.id, missed.id}


def test_trigram_search_tolerates_index_catching_up_mid_search(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    conversation = store.create_conversation(title="general")
    _append(store, conversation.id, "raceneedle")
    monkeypatch.setattr(sqlite_trigram, "_schedule_catch_up", sqlite_trigram._catch_up)

    assert _search_ids(store, "raceneedle") == {conversation.id}
