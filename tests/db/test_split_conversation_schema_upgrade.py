"""Regression tests for versioned split conversation-database upgrades."""

from __future__ import annotations

import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import sqlalchemy as sa

from omnigent.db import ConversationBase
from omnigent.db.utils import _run_conversation_schema_upgrades, clear_engine_cache
from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


def _initialize_store_process(main_uri: str, split_uri: str) -> tuple[bool, bool]:
    """Initialize one store in a fresh process and report split schema state."""
    clear_engine_cache()
    store = SqlAlchemyConversationStore(main_uri, split_uri)
    inspector = sa.inspect(store._conv_engine)
    columns = {column["name"] for column in inspector.get_columns("conversation_items")}
    indexes = {index["name"] for index in inspector.get_indexes("conversation_items")}
    return (
        "source_id" in columns,
        "ix_conversation_items_source_id" in indexes,
    )


def _create_legacy_split_database(split_uri: str) -> None:
    """Create the pre-source-id split schema used by upgrade regressions."""
    legacy_engine = sa.create_engine(split_uri)
    ConversationBase.metadata.create_all(legacy_engine)
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX ix_conversation_items_source_id")
        connection.exec_driver_sql("ALTER TABLE conversation_items DROP COLUMN source_id")
    legacy_engine.dispose()


def test_preexisting_split_database_upgrades_before_append(tmp_path: Path) -> None:
    """A legacy split DB gains source identity before normal store writes."""
    main_uri = f"sqlite:///{tmp_path / 'main.db'}"
    split_uri = f"sqlite:///{tmp_path / 'conversations.db'}"
    _create_legacy_split_database(split_uri)
    legacy_engine = sa.create_engine(split_uri)
    assert "source_id" not in {
        column["name"] for column in sa.inspect(legacy_engine).get_columns("conversation_items")
    }
    legacy_engine.dispose()

    clear_engine_cache()
    store = SqlAlchemyConversationStore(main_uri, split_uri)
    conversation = store.create_conversation()
    persisted = store.append(
        conversation.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_split_upgrade",
                source_id="legacy-split-source:0:message",
                data=MessageData(
                    role="assistant",
                    agent="claude-code",
                    content=[{"type": "output_text", "text": "after split upgrade"}],
                ),
            )
        ],
    )

    assert len(persisted) == 1
    _run_conversation_schema_upgrades(store._conv_engine)
    inspector = sa.inspect(store._conv_engine)
    assert "source_id" in {
        column["name"] for column in inspector.get_columns("conversation_items")
    }
    assert "ix_conversation_items_source_id" in {
        index["name"] for index in inspector.get_indexes("conversation_items")
    }
    assert "omnigent_conversation_schema_migrations" in inspector.get_table_names()
    with store._conv_engine.connect() as connection:
        versions = connection.execute(
            sa.text("SELECT version FROM omnigent_conversation_schema_migrations")
        ).scalars()
        assert list(versions) == [1]


def test_split_upgrade_replaces_wrong_shape_source_index(tmp_path: Path) -> None:
    """A legacy same-name index is validated rather than silently stamped."""
    main_uri = f"sqlite:///{tmp_path / 'main-wrong-index.db'}"
    split_uri = f"sqlite:///{tmp_path / 'conversations-wrong-index.db'}"
    legacy_engine = sa.create_engine(split_uri)
    ConversationBase.metadata.create_all(legacy_engine)
    with legacy_engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX ix_conversation_items_source_id")
        connection.exec_driver_sql(
            "CREATE INDEX ix_conversation_items_source_id ON conversation_items (conversation_id)"
        )
    legacy_engine.dispose()

    clear_engine_cache()
    store = SqlAlchemyConversationStore(main_uri, split_uri)
    indexes = {
        index["name"]: index
        for index in sa.inspect(store._conv_engine).get_indexes("conversation_items")
    }
    source_index = indexes["ix_conversation_items_source_id"]
    assert tuple(source_index["column_names"]) == (
        "workspace_id",
        "conversation_id",
        "source_id",
    )
    assert "source_id IS NOT NULL" in str(source_index["dialect_options"]["sqlite_where"])


def test_concurrent_processes_serialize_split_database_upgrade(tmp_path: Path) -> None:
    """Multiple server processes can initialize one legacy split database."""
    split_uri = f"sqlite:///{tmp_path / 'shared-conversations.db'}"
    _create_legacy_split_database(split_uri)
    context = multiprocessing.get_context("spawn")
    with ProcessPoolExecutor(max_workers=4, mp_context=context) as executor:
        futures = [
            executor.submit(
                _initialize_store_process,
                f"sqlite:///{tmp_path / f'main-{index}.db'}",
                split_uri,
            )
            for index in range(4)
        ]
        assert [future.result(timeout=60) for future in futures] == [
            (True, True),
            (True, True),
            (True, True),
            (True, True),
        ]

    engine = sa.create_engine(split_uri)
    inspector = sa.inspect(engine)
    assert "source_id" in {
        column["name"] for column in inspector.get_columns("conversation_items")
    }
    assert "ix_conversation_items_source_id" in {
        index["name"] for index in inspector.get_indexes("conversation_items")
    }
    with engine.connect() as connection:
        versions = connection.execute(
            sa.text("SELECT version FROM omnigent_conversation_schema_migrations")
        ).scalars()
        assert list(versions) == [1]
    engine.dispose()
