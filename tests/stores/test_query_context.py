"""Tests for semantic database query names."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import Engine, event

from omnigent.db import current_query_name, query_name_scope
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore


@contextmanager
def _capture_application_query_names(*engines: Engine) -> Iterator[list[str | None]]:
    """Capture names visible while application DML executes on ``engines``."""
    query_names: list[str | None] = []

    def capture_query_name(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        command = statement.lstrip().partition(" ")[0].upper()
        if command in {"SELECT", "INSERT", "UPDATE", "DELETE"}:
            query_names.append(current_query_name())

    unique_engines = list(dict.fromkeys(engines))
    for engine in unique_engines:
        event.listen(engine, "before_cursor_execute", capture_query_name)
    try:
        yield query_names
    finally:
        for engine in unique_engines:
            event.remove(engine, "before_cursor_execute", capture_query_name)


def test_query_name_scope_restores_nested_and_failed_scopes() -> None:
    """Nested and failed queries cannot leak their names to later work."""
    assert current_query_name() is None

    with query_name_scope("outer.query"):
        assert current_query_name() == "outer.query"
        with pytest.raises(RuntimeError, match="failed query"):
            with query_name_scope("inner.query"):
                assert current_query_name() == "inner.query"
                raise RuntimeError("failed query")
        assert current_query_name() == "outer.query"

    assert current_query_name() is None


def test_query_name_scope_rejects_empty_names() -> None:
    """An empty identifier fails before any query can inherit it."""
    with pytest.raises(ValueError, match="query_name must not be empty"):
        with query_name_scope("  "):
            raise AssertionError("empty query name entered its scope")


def test_file_store_names_each_application_query(db_uri: str) -> None:
    """Every query in a file-store lifecycle exposes its stable semantic name."""
    file_store = SqlAlchemyFileStore(db_uri)

    with _capture_application_query_names(file_store._engine) as query_names:
        file = file_store.create("report.txt", 10, session_id="94c349190e241f85a984b3df8f129696")
        assert query_names == ["omnigent.file_store.insert_file"]

        query_names.clear()
        file_store.get(file.id)
        assert query_names == ["omnigent.file_store.select_file_by_id"]

        query_names.clear()
        file_store.list(session_id="94c349190e241f85a984b3df8f129696")
        assert query_names == ["omnigent.file_store.list_files"]

        query_names.clear()
        file_store.delete(file.id)
        assert query_names == [
            "omnigent.file_store.select_file_by_id",
            "omnigent.file_store.delete_file",
        ]

        file_store.create("old.txt", 1, session_id="94c349190e241f85a984b3df8f129696")
        query_names.clear()
        file_store.delete_all_for_session("94c349190e241f85a984b3df8f129696")
        assert query_names == [
            "omnigent.file_store.list_session_files_for_delete",
            "omnigent.file_store.delete_session_files",
        ]


def test_conversation_store_names_create_and_get_queries(
    split_db_conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Conversation creation and lookup name queries across both databases."""
    store = split_db_conversation_store

    with _capture_application_query_names(store._engine, store._conv_engine) as query_names:
        parent = store.create_conversation(title="parent")
        assert query_names == [
            "omnigent.conversation_store.insert_conversation",
            "omnigent.conversation_store.insert_conversation_metadata",
        ]

        query_names.clear()
        child = store.create_conversation(
            title="child",
            parent_conversation_id=parent.id,
        )
        assert query_names == [
            "omnigent.conversation_store.select_parent_conversation",
            "omnigent.conversation_store.select_duplicate_child_title",
            "omnigent.conversation_store.insert_conversation",
            "omnigent.conversation_store.insert_conversation_metadata",
        ]

        query_names.clear()
        fetched = store.get_conversation(child.id)
        assert fetched is not None
        assert fetched.id == child.id
        assert query_names == [
            "omnigent.conversation_store.select_conversation_by_id",
            "omnigent.conversation_store.select_conversation_metadata_by_id",
            "omnigent.conversation_store.select_conversation_labels",
        ]
