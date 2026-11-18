"""Tests for semantic database query names."""

from __future__ import annotations

import ast
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import Engine, event

from omnigent.db import current_query_name, query_name_scope
from omnigent.db.db_models import SqlPreference, current_workspace_id
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

_REPOSITORY_ROOT = Path(__file__).parents[2]
_APPLICATION_STORE_PATHS = [
    *_REPOSITORY_ROOT.glob("omnigent/stores/*/sqlalchemy_store.py"),
    _REPOSITORY_ROOT / "omnigent/stores/host_store.py",
    _REPOSITORY_ROOT / "omnigent/server/accounts_store.py",
    _REPOSITORY_ROOT / "omnigent/server/device_grant_store.py",
]


@contextmanager
def _capture_application_query_names(
    *engines: Engine, include_commits: bool = False
) -> Iterator[list[str | None]]:
    """Capture names visible during application DML and optional commits."""
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

    def capture_commit_name(_connection: object) -> None:
        query_names.append(current_query_name())

    unique_engines = list(dict.fromkeys(engines))
    for engine in unique_engines:
        event.listen(engine, "before_cursor_execute", capture_query_name)
        if include_commits:
            event.listen(engine, "commit", capture_commit_name)
    try:
        yield query_names
    finally:
        for engine in unique_engines:
            event.remove(engine, "before_cursor_execute", capture_query_name)
            if include_commits:
                event.remove(engine, "commit", capture_commit_name)


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


def test_application_store_sessions_require_literal_query_names() -> None:
    """New store transactions cannot silently bypass semantic query naming."""
    unnamed_calls: list[str] = []
    legacy_makers: list[str] = []

    for path in _APPLICATION_STORE_PATHS:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if isinstance(node.func, ast.Name) and node.func.id == "make_managed_session_maker":
                legacy_makers.append(f"{path.relative_to(_REPOSITORY_ROOT)}:{node.lineno}")
            if (
                isinstance(node.func, ast.Name)
                and node.func.id.endswith("session_maker")
                and not node.args
            ):
                unnamed_calls.append(f"{path.relative_to(_REPOSITORY_ROOT)}:{node.lineno}")
            if not (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "self"
                and node.func.attr
                in {"_session", "_session_immediate", "_conv_session", "_conv_session_immediate"}
            ):
                continue
            if not (
                len(node.args) == 1
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
                and node.args[0].value.strip()
            ):
                unnamed_calls.append(f"{path.relative_to(_REPOSITORY_ROOT)}:{node.lineno}")

    assert legacy_makers == []
    assert unnamed_calls == []


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
            "omnigent.file_store.count_blob_key_refs",
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
            "omnigent.conversation_store.select_conversation_metadata_by_id",
            "omnigent.conversation_store.select_duplicate_child_title",
            # Read the parent's automation origin and run before inserting its child.
            "omnigent.conversation_store.insert_conversation",
            "omnigent.conversation_store.insert_conversation",
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


def test_conversation_store_names_split_database_nest_operations(
    split_db_conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Nest operations retain semantic names through the separate label commit."""
    store = split_db_conversation_store
    source = store.create_conversation()
    sibling = store.create_conversation()
    moved = store.create_conversation()

    with _capture_application_query_names(store._conv_engine, include_commits=True) as query_names:
        store.add_new_sibling(source.id, sibling.id, user_id=None)
        store.set_session_nest(moved.id, source.id, user_id=None)
        store.set_family_pin(source.id, "123", user_id=None)
        store.set_conversation_project(source.id, "1" * 32)
        with store._session_immediate("seed_pending_nest_labels") as session:
            session.add(
                SqlPreference(
                    workspace_id=current_workspace_id(),
                    user_id="__session_nest_repairs__",
                    key=moved.id,
                    value=json.dumps({"family_id": source.id, "user_id": None}),
                )
            )
        store._repair_pending_nest_labels(moved.id)
        store.set_session_nest(moved.id, None, user_id=None)

    assert set(query_names) == {
        f"omnigent.conversation_store.{operation}"
        for operation in (
            "select_conversation_by_id",
            "select_conversation_labels",
            "create_sibling_membership",
            "copy_sibling_labels",
            "move_session_nest",
            "copy_session_nest_labels",
            "pin_session_family",
            "move_session_family",
            "clear_family_project_labels",
            "repair_pending_nest_labels",
        )
    }
