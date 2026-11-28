"""The SQLite trigram index covers every item, follows item writes, and resumes its backfill."""

from __future__ import annotations

from collections.abc import Callable
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.utils import get_or_create_engine
from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

migration = import_module(
    "omnigent.db.migrations.versions.ce23c8d4c682_conversation_items_trigram_index"
)
_FTS = "conversation_items_trigram_fts"
_ROWS = "conversation_items_trigram_rows"


def _migrate(engine: sa.Engine, operation: Callable[[], None]) -> None:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        # The per-migration transaction Alembic opens on SQLite, which the
        # backfill's autocommit block commits before batching.
        with Operations.context(context), context.begin_transaction(_per_migration=True):
            operation()
        connection.commit()


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


def _items(connection: sa.Connection) -> set[tuple[bytes, bytes]]:
    return set(
        connection.exec_driver_sql("SELECT conversation_id, id FROM conversation_items").tuples()
    )


def _indexed(connection: sa.Connection) -> set[tuple[bytes, bytes]]:
    rows = connection.exec_driver_sql(f"SELECT conversation_id, item_id FROM {_ROWS}").all()
    documents = connection.exec_driver_sql(f"SELECT COUNT(*) FROM {_FTS}_docsize").scalar_one()
    assert documents == len(rows)
    connection.exec_driver_sql(f"INSERT INTO {_FTS}({_FTS}, rank) VALUES ('integrity-check', 0)")
    return {(row.conversation_id, row.item_id) for row in rows}


def _matching_items(connection: sa.Connection, text: str) -> set[bytes]:
    return set(
        connection.exec_driver_sql(
            f"SELECT item_id FROM {_FTS} JOIN {_ROWS} ON fts_rowid = {_FTS}.rowid "
            f"WHERE {_FTS} MATCH ?",
            ('"' + text + '"',),
        ).scalars()
    )


def _item_ids(connection: sa.Connection, text: str) -> set[bytes]:
    return set(
        connection.exec_driver_sql(
            "SELECT id FROM conversation_items WHERE search_text LIKE ?", (f"%{text}%",)
        ).scalars()
    )


@pytest.fixture()
def store(db_uri: str) -> SqlAlchemyConversationStore:
    if get_or_create_engine(db_uri).dialect.name != "sqlite":
        pytest.skip("the trigram index exists only on SQLite")
    return SqlAlchemyConversationStore(db_uri)


def test_trigram_index_backfills_and_follows_item_writes(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = store._conv_engine
    kept = store.create_conversation(title="kept")
    _append(store, kept.id, "alpha sandbox", "beta")
    _migrate(engine, migration.downgrade)
    assert not sa.inspect(engine).has_table(_ROWS)

    # Written while the index is absent, then indexed by the backfill in small batches.
    removed = store.create_conversation(title="removed")
    _append(store, removed.id, "gamma sandbox", "ÄBC", "delta")
    monkeypatch.setattr(migration, "_BACKFILL_BATCH_ITEMS", 2)
    _migrate(engine, migration.upgrade)
    _migrate(engine, migration.upgrade)
    with engine.connect() as connection:
        assert _indexed(connection) == _items(connection)
        assert _matching_items(connection, "andbox") == _item_ids(connection, "andbox")
        assert len(_matching_items(connection, "äbc")) == 1

    added = store.create_conversation(title="added")
    _append(store, added.id, "epsilon sandbox")
    fork = store.fork_conversation(kept.id)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "DELETE FROM conversation_items WHERE conversation_id = ?",
            (bytes.fromhex(removed.id),),
        )
        connection.exec_driver_sql(
            "UPDATE conversation_items SET search_text = 'zeta' WHERE search_text = 'beta' "
            "AND conversation_id = ?",
            (bytes.fromhex(fork.id),),
        )
    with engine.connect() as connection:
        assert _indexed(connection) == _items(connection)
        assert _matching_items(connection, "andbox") == _item_ids(connection, "andbox")
        assert len(_item_ids(connection, "andbox")) == 3
        assert _matching_items(connection, "zeta") == _item_ids(connection, "zeta") != set()
        assert _matching_items(connection, "gamma") == set()

    # The newest item's FTS rowid is reused after it is deleted.
    with engine.begin() as connection:
        newest = connection.exec_driver_sql(
            f"SELECT item_id FROM {_ROWS} ORDER BY fts_rowid DESC LIMIT 1"
        ).scalar_one()
        connection.exec_driver_sql("DELETE FROM conversation_items WHERE id = ?", (newest,))
    _append(store, added.id, "omega")
    with engine.connect() as connection:
        assert _indexed(connection) == _items(connection)
        assert _matching_items(connection, "omega") == _item_ids(connection, "omega")
        assert newest not in _matching_items(connection, "andbox")

    _migrate(engine, migration.downgrade)
    with engine.connect() as connection:
        assert not connection.exec_driver_sql(
            "SELECT name FROM sqlite_master WHERE name LIKE '%trigram%'"
        ).all()


def test_trigram_backfill_resumes_after_an_interrupted_upgrade(
    store: SqlAlchemyConversationStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = store._conv_engine
    conversation = store.create_conversation(title="resumed")
    _migrate(engine, migration.downgrade)
    _append(store, conversation.id, *(f"needle {number}" for number in range(5)))
    monkeypatch.setattr(migration, "_BACKFILL_BATCH_ITEMS", 2)
    batch_end = migration._batch_end
    calls: list[int] = []

    def interrupt_second_batch(rows):
        calls.append(len(rows))
        if len(calls) == 2:
            raise RuntimeError("interrupted")
        return batch_end(rows)

    monkeypatch.setattr(migration, "_batch_end", interrupt_second_batch)
    with pytest.raises(RuntimeError, match="interrupted"):
        _migrate(engine, migration.upgrade)
    with engine.connect() as connection:
        assert len(_indexed(connection)) == 2

    monkeypatch.setattr(migration, "_batch_end", batch_end)
    _migrate(engine, migration.upgrade)
    with engine.connect() as connection:
        assert _indexed(connection) == _items(connection)
        assert len(_matching_items(connection, "needle")) == 5


def test_trigram_backfill_batches_are_bounded_by_text_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(migration, "_BACKFILL_BATCH_BYTES", 10)
    rows = [(0, b"c", bytes([item]), 4) for item in range(5)]
    assert migration._batch_end(rows) == (0, b"c", bytes([2]))
    assert migration._batch_end(rows[:2]) == (0, b"c", bytes([1]))
