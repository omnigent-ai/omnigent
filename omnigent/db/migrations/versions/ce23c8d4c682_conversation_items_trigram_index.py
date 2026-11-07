"""Add a trigram index of conversation item text on SQLite.

Session search matches ``search_text`` as a case-insensitive substring. On
SQLite that reads every item, so search time grows with history. This adds a
derived FTS5 trigram index that search can use to find candidate items.

SQLite 3.43+ only (trigram tokenizer plus contentless deletes); other dialects
and older SQLite builds skip it, and search keeps its LIKE scan there.

- ``conversation_items_trigram_fts`` is contentless: it stores the index, not
  the text, and deletes by rowid without the old text.
- ``conversation_items_trigram_rows`` maps each indexed item to its FTS rowid.
- Triggers on ``conversation_items`` update both in the writer's transaction,
  so every binary version that writes items keeps the index current.

Existing items are indexed in bounded batches, one transaction each. An
interrupted upgrade resumes: indexed items are skipped. Deploy before the
search change that reads the index; that change falls back to LIKE whenever
these objects are missing. Downgrade drops the triggers and both tables.

A migration that rebuilds ``conversation_items`` on SQLite (``batch_alter_table``
copies the table) drops these triggers and must re-run this upgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ce23c8d4c682"
down_revision: str | None = "f1c7a9e2b406"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MIN_SQLITE_VERSION = (3, 43, 0)
_FTS_TABLE = "conversation_items_trigram_fts"
_ROWS_TABLE = "conversation_items_trigram_rows"
_TRIGGERS = (
    "conversation_items_trigram_insert",
    "conversation_items_trigram_update",
    "conversation_items_trigram_delete",
)
# Each backfill transaction indexes at most this many items or text bytes.
_BACKFILL_BATCH_ITEMS = 1000
_BACKFILL_BATCH_BYTES = 4 * 1024 * 1024

_CREATE_TABLES = (
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {_FTS_TABLE} USING fts5("
    "search_text, content='', contentless_delete=1, tokenize='trigram')",
    f"CREATE TABLE IF NOT EXISTS {_ROWS_TABLE} ("
    "workspace_id BIGINT NOT NULL, "
    "conversation_id BLOB NOT NULL, "
    "item_id BLOB NOT NULL, "
    "fts_rowid INTEGER NOT NULL, "
    f"CONSTRAINT pk_{_ROWS_TABLE} PRIMARY KEY (workspace_id, conversation_id, item_id)"
    ") WITHOUT ROWID",
    f"CREATE UNIQUE INDEX IF NOT EXISTS ix_{_ROWS_TABLE}_fts_rowid ON {_ROWS_TABLE} (fts_rowid)",
)


def _row_of(item: str) -> str:
    return (
        f"workspace_id = {item}.workspace_id AND conversation_id = {item}.conversation_id "
        f"AND item_id = {item}.id"
    )


def _unindex(item: str) -> str:
    """Statements removing *item* (``old`` or ``new``) from the index, if present."""
    return (
        f"DELETE FROM {_FTS_TABLE} WHERE rowid = "
        f"(SELECT fts_rowid FROM {_ROWS_TABLE} WHERE {_row_of(item)}); "
        f"DELETE FROM {_ROWS_TABLE} WHERE {_row_of(item)}; "
    )


# A new FTS rowid is one past the largest in use. Reusing a deleted item's rowid
# is safe: its FTS entry is removed in the same transaction as its mapping.
_INDEX_NEW = (
    f"INSERT INTO {_ROWS_TABLE} (workspace_id, conversation_id, item_id, fts_rowid) "
    "SELECT new.workspace_id, new.conversation_id, new.id, "
    f"COALESCE((SELECT MAX(fts_rowid) FROM {_ROWS_TABLE}), 0) + 1 "
    "WHERE new.search_text IS NOT NULL; "
    f"INSERT INTO {_FTS_TABLE} (rowid, search_text) "
    f"SELECT fts_rowid, new.search_text FROM {_ROWS_TABLE} WHERE {_row_of('new')}; "
)

# Unindexing first makes the insert trigger safe for a key that is already mapped
# (an INSERT OR REPLACE skips delete triggers).
_CREATE_TRIGGERS = (
    f"CREATE TRIGGER IF NOT EXISTS {_TRIGGERS[0]} AFTER INSERT ON conversation_items "
    f"BEGIN {_unindex('new')}{_INDEX_NEW}END",
    f"CREATE TRIGGER IF NOT EXISTS {_TRIGGERS[1]} "
    "AFTER UPDATE OF workspace_id, conversation_id, id, search_text ON conversation_items "
    f"BEGIN {_unindex('old')}{_unindex('new')}{_INDEX_NEW}END",
    f"CREATE TRIGGER IF NOT EXISTS {_TRIGGERS[2]} AFTER DELETE ON conversation_items "
    f"BEGIN {_unindex('old')}END",
)

_ITEM_KEY = "(workspace_id, conversation_id, id)"
_NEXT_BATCH = (
    "SELECT workspace_id, conversation_id, id, COALESCE(length(CAST(search_text AS BLOB)), 0) "
    f"FROM conversation_items WHERE {_ITEM_KEY} > (?, ?, ?) "
    "ORDER BY workspace_id, conversation_id, id LIMIT ?"
)
_UNINDEXED_IN_BATCH = (
    "FROM conversation_items AS item "
    f"WHERE {_ITEM_KEY} > (?, ?, ?) AND {_ITEM_KEY} <= (?, ?, ?) "
    "AND item.search_text IS NOT NULL "
    f"AND NOT EXISTS (SELECT 1 FROM {_ROWS_TABLE} AS indexed "
    "WHERE indexed.workspace_id = item.workspace_id "
    "AND indexed.conversation_id = item.conversation_id AND indexed.item_id = item.id)"
)


def _supported(connection: sa.Connection) -> bool:
    if connection.dialect.name != "sqlite":
        return False
    version = connection.exec_driver_sql("SELECT sqlite_version()").scalar_one()
    return tuple(int(part) for part in version.split(".")[:3]) >= _MIN_SQLITE_VERSION


def _batch_end(rows: Sequence[sa.Row]) -> tuple[int, bytes, bytes]:
    """Key of the last row that keeps the batch within the byte budget."""
    total = 0
    for row in rows:
        total += row[3]
        if total >= _BACKFILL_BATCH_BYTES:
            return row[0], row[1], row[2]
    return rows[-1][0], rows[-1][1], rows[-1][2]


def _backfill() -> None:
    """Index items written before the triggers existed, one bounded batch at a time."""
    # Below every real key: workspace ids are non-negative.
    cursor: tuple[int, bytes, bytes] = (-1, b"", b"")
    while True:
        connection = op.get_bind()
        rows = connection.exec_driver_sql(_NEXT_BATCH, (*cursor, _BACKFILL_BATCH_ITEMS)).all()
        if not rows:
            return
        batch_end = _batch_end(rows)
        first_rowid = connection.exec_driver_sql(
            f"SELECT COALESCE(MAX(fts_rowid), 0) FROM {_ROWS_TABLE}"
        ).scalar_one()
        connection.exec_driver_sql(
            f"INSERT INTO {_ROWS_TABLE} (workspace_id, conversation_id, item_id, fts_rowid) "
            "SELECT workspace_id, conversation_id, id, "
            f"? + row_number() OVER (ORDER BY workspace_id, conversation_id, id) "
            f"{_UNINDEXED_IN_BATCH}",
            (first_rowid, *cursor, *batch_end),
        )
        connection.exec_driver_sql(
            f"INSERT INTO {_FTS_TABLE} (rowid, search_text) "
            f"SELECT indexed.fts_rowid, item.search_text FROM {_ROWS_TABLE} AS indexed "
            "JOIN conversation_items AS item ON item.workspace_id = indexed.workspace_id "
            "AND item.conversation_id = indexed.conversation_id AND item.id = indexed.item_id "
            "WHERE indexed.fts_rowid > ?",
            (first_rowid,),
        )
        if len(rows) < _BACKFILL_BATCH_ITEMS and batch_end == tuple(rows[-1][:3]):
            return  # The last batch commits with the revision stamp.
        # Commit this batch so an interrupted upgrade keeps it.
        with op.get_context().autocommit_block():
            pass
        cursor = batch_end


def upgrade() -> None:
    """Create the trigram index and its triggers, then index existing items."""
    if not _supported(op.get_bind()):
        return
    # Triggers come first so items written during the backfill are indexed too.
    for statement in (*_CREATE_TABLES, *_CREATE_TRIGGERS):
        op.execute(statement)
    _backfill()


def downgrade() -> None:
    """Drop the triggers and the trigram index."""
    if op.get_bind().dialect.name != "sqlite":
        return
    for trigger in _TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger}")
    op.execute(f"DROP TABLE IF EXISTS {_ROWS_TABLE}")
    op.execute(f"DROP TABLE IF EXISTS {_FTS_TABLE}")
