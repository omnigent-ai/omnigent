"""Atomic model-selection resets preserve concurrent session settings."""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import event, select, update
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.engine.interfaces import Dialect
from sqlalchemy.sql.dml import Update

from omnigent.db import current_query_name
from omnigent.db.db_models import (
    SqlConversation,
    SqlConversationMetadata,
    current_workspace_id,
    workspace_scope,
)
from omnigent.stores.conversation_store import sqlalchemy_store
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture(params=["conversation_store", "split_db_conversation_store"])
def store(request: pytest.FixtureRequest) -> SqlAlchemyConversationStore:
    """Exercise both co-located and separate conversation/metadata databases."""
    return request.getfixturevalue(request.param)


def _write_overrides(
    store: SqlAlchemyConversationStore,
    conversation_id: str,
    raw: str | None,
    *,
    updated_at: int = 100,
) -> None:
    with store._conv_engine.begin() as connection:
        connection.execute(
            update(SqlConversation)
            .where(
                SqlConversation.workspace_id == current_workspace_id(),
                SqlConversation.id == conversation_id,
            )
            .values(session_overrides=raw, updated_at=updated_at)
        )


def _snapshot(
    store: SqlAlchemyConversationStore, conversation_id: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    with store._conv_engine.connect() as connection:
        conversation = (
            connection.execute(
                select(SqlConversation.__table__).where(
                    SqlConversation.workspace_id == current_workspace_id(),
                    SqlConversation.id == conversation_id,
                )
            )
            .mappings()
            .one()
        )
    with store._engine.connect() as connection:
        metadata = (
            connection.execute(
                select(SqlConversationMetadata.__table__).where(
                    SqlConversationMetadata.workspace_id == current_workspace_id(),
                    SqlConversationMetadata.id == conversation_id,
                )
            )
            .mappings()
            .one()
        )
    return dict(conversation), dict(metadata)


@pytest.mark.parametrize("with_siblings", [False, True], ids=["only-model", "sibling-settings"])
def test_clear_matching_model_preserves_other_settings(
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    with_siblings: bool,
) -> None:
    conversation = store.create_conversation(
        title="Keep this title", runner_id="runner-original", workspace="/tmp/model-reset"
    )
    store.update_conversation(conversation.id, terminal_launch_args=["--existing"])
    store.set_labels(conversation.id, {"keep": "label"})
    siblings = (
        {
            "reasoning_effort": "high",
            "reported_model": "last-reported-model",
            "cost_control_mode_override": "off",
            "subagent_routing_override": "on",
            "harness_override": "codex-native",
            "future_setting": {"keep": True},
        }
        if with_siblings
        else {}
    )
    _write_overrides(
        store, conversation.id, json.dumps({"model_override": "unavailable-model", **siblings})
    )
    before, metadata_before = _snapshot(store, conversation.id)
    monkeypatch.setattr(sqlalchemy_store, "now_epoch", lambda: 200)

    assert store.clear_model_override_if_matches(conversation.id, "unavailable-model") is True

    after, metadata_after = _snapshot(store, conversation.id)
    encoded = json.dumps(siblings, separators=(",", ":")) if siblings else None
    assert after == {**before, "session_overrides": encoded, "updated_at": 200}
    assert metadata_after == metadata_before
    fetched = store.get_conversation(conversation.id)
    assert fetched is not None
    assert fetched.model_override is None
    assert fetched.labels == {"keep": "label"}
    monkeypatch.setattr(sqlalchemy_store, "now_epoch", lambda: 300)
    assert store.clear_model_override_if_matches(conversation.id, "unavailable-model") is False
    assert _snapshot(store, conversation.id) == (after, metadata_after)


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "{}",
        '{"model_override":null}',
        '{ "model_override": "newer-model", "reasoning_effort": "high" }',
        '{"reasoning_effort":"high"}',
    ],
    ids=["sql-null", "empty-object", "json-null", "different-model", "missing-model"],
)
def test_clear_nonmatching_model_is_a_complete_noop(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch, raw: str | None
) -> None:
    conversation = store.create_conversation(title="Unchanged")
    _write_overrides(store, conversation.id, raw)
    before = _snapshot(store, conversation.id)
    monkeypatch.setattr(sqlalchemy_store, "now_epoch", lambda: 200)

    assert store.clear_model_override_if_matches(conversation.id, "unavailable-model") is False

    assert _snapshot(store, conversation.id) == before


def test_clear_missing_conversation_is_a_noop(store: SqlAlchemyConversationStore) -> None:
    conversation = store.create_conversation(title="Unrelated")
    before = _snapshot(store, conversation.id)

    assert store.clear_model_override_if_matches("f" * 32, "unavailable-model") is False

    assert _snapshot(store, conversation.id) == before


@pytest.mark.parametrize(
    "replacement",
    [
        {"model_override": "newer-model", "reasoning_effort": "high"},
        {"model_override": "UNAVAILABLE-MODEL", "reasoning_effort": "high"},
        {"model_override": "unavailable-model", "reasoning_effort": "low"},
        {"reasoning_effort": "high"},
    ],
    ids=["newer-model", "case-only-model-change", "sibling-setting", "already-cleared"],
)
def test_clear_cannot_overwrite_a_competing_settings_update(
    store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    replacement: dict[str, str],
) -> None:
    conversation = store.create_conversation(title="Keep title")
    _write_overrides(
        store,
        conversation.id,
        json.dumps({"model_override": "unavailable-model", "reasoning_effort": "high"}),
    )
    before, metadata_before = _snapshot(store, conversation.id)
    replacement_raw = json.dumps(replacement)
    replaced = False

    def _competing_write(
        _connection: object,
        statement: object,
        _multiparams: object,
        _params: object,
        _execution_options: object,
    ) -> None:
        nonlocal replaced
        if replaced or not isinstance(statement, Update):
            return
        if statement.table.name != SqlConversation.__tablename__:
            return
        replaced = True
        _write_overrides(store, conversation.id, replacement_raw, updated_at=150)

    monkeypatch.setattr(sqlalchemy_store, "now_epoch", lambda: 200)
    event.listen(store._conv_engine, "before_execute", _competing_write)
    try:
        assert store.clear_model_override_if_matches(conversation.id, "unavailable-model") is False
    finally:
        event.remove(store._conv_engine, "before_execute", _competing_write)

    assert replaced, "the competing write must happen after the read and before the CAS"
    assert _snapshot(store, conversation.id) == (
        {**before, "session_overrides": replacement_raw, "updated_at": 150},
        metadata_before,
    )


@pytest.mark.parametrize("same_overrides", [False, True], ids=["different-blobs", "same-blob"])
def test_clear_model_override_is_workspace_scoped(
    store: SqlAlchemyConversationStore, same_overrides: bool
) -> None:
    conversation_id = "a" * 32
    raw = '{"model_override":"unavailable-model","reasoning_effort":"high"}'
    with workspace_scope(11):
        store.create_conversation(conversation_id=conversation_id, title="Workspace 11")
        _write_overrides(store, conversation_id, raw)
        untouched = _snapshot(store, conversation_id)
    with workspace_scope(22):
        store.create_conversation(conversation_id=conversation_id, title="Workspace 22")
        _write_overrides(
            store, conversation_id, raw if same_overrides else raw.replace("high", "low")
        )

    assert store.clear_model_override_if_matches(conversation_id, "unavailable-model") is False
    with workspace_scope(22):
        assert store.clear_model_override_if_matches(conversation_id, "unavailable-model") is True
        fetched = store.get_conversation(conversation_id)
        assert fetched is not None
        assert fetched.model_override is None
        assert fetched.reasoning_effort == ("high" if same_overrides else "low")
    with workspace_scope(11):
        assert _snapshot(store, conversation_id) == untouched


def test_clear_model_override_uses_one_semantic_query_name(
    store: SqlAlchemyConversationStore,
) -> None:
    conversation = store.create_conversation()
    _write_overrides(store, conversation.id, '{"model_override":"unavailable-model"}')
    queries: list[tuple[str, str | None]] = []

    def _capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _params: object,
        _context: object,
        _many: bool,
    ) -> None:
        kind = statement.split(maxsplit=1)[0].upper()
        if kind in {"SELECT", "UPDATE"}:
            queries.append((kind, current_query_name()))

    event.listen(store._conv_engine, "before_cursor_execute", _capture)
    try:
        assert store.clear_model_override_if_matches(conversation.id, "unavailable-model") is True
    finally:
        event.remove(store._conv_engine, "before_cursor_execute", _capture)

    assert {kind for kind, _ in queries} == {"SELECT", "UPDATE"}
    assert {name for _, name in queries} == {
        "omnigent.conversation_store.clear_model_override_if_matches"
    }


@pytest.mark.parametrize(
    "dialect",
    [sqlite.dialect(), postgresql.dialect(), mysql.dialect()],
    ids=["sqlite", "postgres", "mysql"],
)
def test_clear_model_override_compiles_portable_exact_blob_comparison(
    conversation_store: SqlAlchemyConversationStore,
    monkeypatch: pytest.MonkeyPatch,
    dialect: Dialect,
) -> None:
    original = '{"model_override":"unavailable-model","future_setting":"é"}'
    statements: list[Update] = []

    class _Session:
        def scalar(self, _statement: object) -> str:
            return original

        def execute(self, statement: Update) -> SimpleNamespace:
            statements.append(statement)
            return SimpleNamespace(rowcount=1)

    @contextmanager
    def _session(query_name: str) -> Iterator[_Session]:
        assert query_name == "clear_model_override_if_matches"
        yield _Session()

    monkeypatch.setattr(conversation_store, "_conv_engine", SimpleNamespace(dialect=dialect))
    monkeypatch.setattr(conversation_store, "_conv_session", _session)

    assert conversation_store.clear_model_override_if_matches("a" * 32, "unavailable-model")

    assert len(statements) == 1
    compiled = statements[0].compile(dialect=dialect)
    sql = str(compiled)
    assert "conversations.workspace_id = " in sql
    assert "conversations.id = " in sql
    if dialect.name == "mysql":
        assert "CAST(conversations.session_overrides AS BINARY) = " in sql
        assert original.encode("utf-8") in compiled.params.values()
    else:
        assert "conversations.session_overrides = " in sql
        assert "CAST(" not in sql
        assert original in compiled.params.values()
