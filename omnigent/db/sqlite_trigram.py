"""SQLite trigram index maintenance for conversation substring search."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Sequence
from typing import Any
from weakref import WeakKeyDictionary

from sqlalchemy import Connection, Engine, Integer, Row, ScalarSelect, Subquery, column, text
from sqlalchemy.orm import Session

from omnigent.db.db_models import current_workspace_id
from omnigent.db.query_context import query_name_scope

_logger = logging.getLogger(__name__)

TRIGRAM_FTS_TABLE = "conversation_items_trigram_fts"
TRIGRAM_ROWS_TABLE = "conversation_items_trigram_rows"
_DELETES_TABLE = "conversation_items_trigram_deletes"
_STATE_TABLE = "conversation_items_trigram_state"
_CANDIDATES_TABLE = "conversation_items_trigram_candidates"
_INSERT_TRIGGER = "conversation_items_trigram_insert"
_DELETE_TRIGGER = "conversation_items_trigram_delete"

_FILL_BATCH_ROWS = 5000
_INDEX_BATCH_ROWS = 100
_INDEX_BATCH_CHARS = 256 * 1024
_BATCH_PAUSE_SECONDS = 0.1
_MAX_UNINDEXED_ROWS = 1000

_CREATE_TABLES = (
    f"CREATE TABLE IF NOT EXISTS {_STATE_TABLE} ("
    "id INTEGER PRIMARY KEY CHECK (id = 1), "
    "filled INTEGER NOT NULL DEFAULT 0, "
    "indexed_through INTEGER NOT NULL DEFAULT 0)",
    f"INSERT OR IGNORE INTO {_STATE_TABLE}(id) VALUES (1)",
    f"CREATE TABLE IF NOT EXISTS {TRIGRAM_ROWS_TABLE} ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "workspace_id INTEGER NOT NULL, "
    "conversation_id BLOB NOT NULL, "
    "item_id BLOB NOT NULL, "
    "position INTEGER NOT NULL, "
    "UNIQUE (workspace_id, conversation_id, item_id))",
    f"CREATE TABLE IF NOT EXISTS {_DELETES_TABLE} ("
    "row_id INTEGER PRIMARY KEY, search_text TEXT NOT NULL)",
    f"CREATE VIRTUAL TABLE IF NOT EXISTS {TRIGRAM_FTS_TABLE} USING fts5("
    "search_text, content='', tokenize='trigram')",
)
_DELETED_ROW = (
    f"{TRIGRAM_ROWS_TABLE}.workspace_id = old.workspace_id "
    f"AND {TRIGRAM_ROWS_TABLE}.conversation_id = old.conversation_id "
    f"AND {TRIGRAM_ROWS_TABLE}.item_id = old.id"
)
_CREATE_TRIGGERS = (
    f"CREATE TRIGGER IF NOT EXISTS {_INSERT_TRIGGER} "
    "AFTER INSERT ON conversation_items WHEN new.search_text IS NOT NULL BEGIN "
    f"INSERT OR IGNORE INTO {TRIGRAM_ROWS_TABLE}"
    "(workspace_id, conversation_id, item_id, position) "
    "VALUES (new.workspace_id, new.conversation_id, new.id, new.position); END",
    f"CREATE TRIGGER IF NOT EXISTS {_DELETE_TRIGGER} "
    "AFTER DELETE ON conversation_items BEGIN "
    f"INSERT INTO {_DELETES_TABLE}(row_id, search_text) "
    f"SELECT id, old.search_text FROM {TRIGRAM_ROWS_TABLE} WHERE {_DELETED_ROW} "
    f"AND id <= (SELECT indexed_through FROM {_STATE_TABLE} WHERE id = 1); "
    f"DELETE FROM {TRIGRAM_ROWS_TABLE} WHERE {_DELETED_ROW}; END",
)
_SELECT_PROGRESS = (
    "SELECT filled, indexed_through, "
    f"(SELECT COALESCE(MAX(id), 0) FROM {TRIGRAM_ROWS_TABLE}) AS last_row_id, "
    f"EXISTS (SELECT 1 FROM {_DELETES_TABLE}) AS has_deletes "
    f"FROM {_STATE_TABLE} WHERE id = 1"
)
_JOIN_INDEXED_ITEMS = (
    "JOIN conversation_items "
    f"ON conversation_items.workspace_id = {TRIGRAM_ROWS_TABLE}.workspace_id "
    f"AND conversation_items.conversation_id = {TRIGRAM_ROWS_TABLE}.conversation_id "
    f"AND conversation_items.id = {TRIGRAM_ROWS_TABLE}.item_id"
)
_MATCH_COLUMNS = (
    f"{TRIGRAM_ROWS_TABLE}.conversation_id, {TRIGRAM_ROWS_TABLE}.position, "
    f"{TRIGRAM_ROWS_TABLE}.item_id"
)
_INDEXED_MATCHES = (
    f"SELECT {_MATCH_COLUMNS} FROM {TRIGRAM_FTS_TABLE} "
    f"CROSS JOIN {TRIGRAM_ROWS_TABLE} ON {TRIGRAM_ROWS_TABLE}.id = {TRIGRAM_FTS_TABLE}.rowid "
    f"WHERE {TRIGRAM_FTS_TABLE} MATCH :fts_match "
    f"AND {TRIGRAM_ROWS_TABLE}.workspace_id = :fts_workspace_id"
)
_UNINDEXED_FILTER = (
    f"{TRIGRAM_ROWS_TABLE}.id > (SELECT indexed_through FROM {_STATE_TABLE} WHERE id = 1) "
    f"AND +{TRIGRAM_ROWS_TABLE}.workspace_id = :fts_workspace_id"
)
_catch_up_lock = threading.Lock()
_catch_up_threads: WeakKeyDictionary[Engine, threading.Thread] = WeakKeyDictionary()


def supports_trigram_fts(engine: Engine) -> bool:
    """Return whether a local SQLite engine provides the trigram tokenizer."""
    version = engine.dialect.server_version_info or ()
    return engine.dialect.name == "sqlite" and version >= (3, 34, 0)


def _has_pending_work(progress: Row[Any]) -> bool:
    return (
        not progress.filled
        or progress.last_row_id > progress.indexed_through
        or bool(progress.has_deletes)
    )


def initialize_trigram_search(engine: Engine) -> None:
    """Create the trigram index and its triggers, and schedule catching it up."""
    if not supports_trigram_fts(engine):
        return

    with query_name_scope("omnigent.database.initialize_trigram_search"), engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        existing = set(conn.exec_driver_sql("SELECT name FROM sqlite_master").scalars())
        for statement in _CREATE_TABLES:
            conn.exec_driver_sql(statement)
        if not {_INSERT_TRIGGER, _DELETE_TRIGGER} <= existing:
            no_items = conn.exec_driver_sql("SELECT 1 FROM conversation_items LIMIT 1").first()
            for statement in _CREATE_TRIGGERS:
                conn.exec_driver_sql(statement)
            conn.exec_driver_sql(
                f"UPDATE {_STATE_TABLE} SET filled = ? WHERE id = 1",
                (int(no_items is None),),
            )
        progress = conn.exec_driver_sql(_SELECT_PROGRESS).one()
        conn.commit()
    if _has_pending_work(progress):
        _schedule_catch_up(engine)


def trigram_match_expression(query: str) -> str | None:
    """Build a MATCH expression from the 3+ character literal runs of a LIKE search."""
    runs = query.replace("_", "%").split("%")
    phrases = ['"' + run.replace('"', '""') + '"' for run in runs if len(run) >= 3]
    return " AND ".join(phrases) if phrases else None


def trigram_search_ready(session: Session) -> bool:
    """Return whether search can use the trigram index; catches it up if behind."""
    bind = session.get_bind()
    engine = bind.engine if isinstance(bind, Connection) else bind
    if not supports_trigram_fts(engine):
        return False

    progress = session.connection().exec_driver_sql(_SELECT_PROGRESS).one()
    if _has_pending_work(progress):
        _schedule_catch_up(engine)
    return bool(progress.filled) and (
        progress.last_row_id - progress.indexed_through <= _MAX_UNINDEXED_ROWS
    )


def load_trigram_candidates(session: Session, match_expression: str) -> None:
    """Load indexed matches and all unindexed items into this connection's temp table."""
    connection = session.connection()
    connection.exec_driver_sql(
        f"CREATE TEMP TABLE IF NOT EXISTS {_CANDIDATES_TABLE} ("
        "conversation_id BLOB NOT NULL, position INTEGER NOT NULL, item_id BLOB NOT NULL, "
        "PRIMARY KEY (conversation_id, position)) WITHOUT ROWID"
    )
    connection.exec_driver_sql(f"DELETE FROM temp.{_CANDIDATES_TABLE}")
    connection.execute(
        text(
            f"INSERT INTO temp.{_CANDIDATES_TABLE}(conversation_id, position, item_id) "
            f"{_INDEXED_MATCHES} UNION ALL "
            f"SELECT {_MATCH_COLUMNS} FROM {TRIGRAM_ROWS_TABLE} WHERE {_UNINDEXED_FILTER}"
        ),
        {"fts_match": match_expression, "fts_workspace_id": current_workspace_id()},
    )


def trigram_literal_match_positions(match_expression: str, like_pattern: str) -> Subquery:
    """Each conversation's earliest item matching a search without ``_`` or ``%``."""
    return (
        text(
            "SELECT conversation_id, MIN(position) AS position FROM ("
            f"{_INDEXED_MATCHES} UNION ALL "
            f"SELECT {_MATCH_COLUMNS} FROM {TRIGRAM_ROWS_TABLE} {_JOIN_INDEXED_ITEMS} "
            f"WHERE {_UNINDEXED_FILTER} "
            "AND conversation_items.search_text LIKE :fts_like_pattern"
            ") GROUP BY conversation_id"
        )
        .bindparams(
            fts_match=match_expression,
            fts_workspace_id=current_workspace_id(),
            fts_like_pattern=like_pattern,
        )
        .columns(column("conversation_id"), column("position", Integer()))
        .subquery("trigram_matches")
    )


def trigram_earliest_like_match_position(like_pattern: str) -> ScalarSelect[int]:
    """Position of the current conversation's first candidate matching *like_pattern*."""
    table = _CANDIDATES_TABLE
    return (
        text(
            f"SELECT {table}.position FROM temp.{table} "
            "JOIN conversation_items "
            "ON conversation_items.workspace_id = conversations.workspace_id "
            f"AND conversation_items.conversation_id = {table}.conversation_id "
            f"AND conversation_items.id = {table}.item_id "
            f"WHERE {table}.conversation_id = conversations.id "
            "AND conversation_items.search_text LIKE :fts_like_pattern "
            f"ORDER BY {table}.position LIMIT 1"
        )
        .bindparams(fts_like_pattern=like_pattern)
        .columns(column("position", Integer()))
        .scalar_subquery()
    )


def _schedule_catch_up(engine: Engine) -> None:
    with _catch_up_lock:
        if (thread := _catch_up_threads.get(engine)) is not None and thread.is_alive():
            return
        _catch_up_threads[engine] = thread = threading.Thread(
            target=_catch_up,
            args=(engine,),
            daemon=True,
            name="omnigent-sqlite-trigram-index",
        )
        thread.start()


def _catch_up(engine: Engine) -> None:
    """Fill the row map if needed, then index it."""
    try:
        _fill_row_map(engine)
        while _index_next_batch(engine):
            time.sleep(_BATCH_PAUSE_SECONDS)
    except Exception:
        _logger.exception("SQLite conversation substring index update failed")


def _fill_row_map(engine: Engine) -> None:
    """Map existing items in short write transactions."""
    last_rowid = 0
    while True:
        with query_name_scope("omnigent.database.fill_trigram_rows"), engine.connect() as conn:
            conn.exec_driver_sql("BEGIN IMMEDIATE")
            if conn.exec_driver_sql(
                f"SELECT filled FROM {_STATE_TABLE} WHERE id = 1"
            ).scalar_one():
                return
            batch_end = conn.exec_driver_sql(
                "SELECT MAX(rowid) FROM (SELECT rowid FROM conversation_items "
                f"WHERE rowid > ? ORDER BY rowid LIMIT {_FILL_BATCH_ROWS})",
                (last_rowid,),
            ).scalar()
            if batch_end is None:
                conn.exec_driver_sql(f"UPDATE {_STATE_TABLE} SET filled = 1 WHERE id = 1")
            else:
                conn.exec_driver_sql(
                    f"INSERT OR IGNORE INTO {TRIGRAM_ROWS_TABLE}"
                    "(workspace_id, conversation_id, item_id, position) "
                    "SELECT workspace_id, conversation_id, id, position FROM conversation_items "
                    "WHERE rowid > ? AND rowid <= ? AND search_text IS NOT NULL",
                    (last_rowid, batch_end),
                )
            conn.commit()
        if batch_end is None:
            return
        last_rowid = batch_end
        time.sleep(_BATCH_PAUSE_SECONDS)


def _batch_end(rows: Sequence[Row[Any]]) -> int | None:
    """Return the last id of ``(id, chars)`` rows that fits the text-size cap."""
    batch_end = None
    batch_chars = 0
    for row_id, chars in rows:
        batch_end = row_id
        batch_chars += chars
        if batch_chars >= _INDEX_BATCH_CHARS:
            break
    return batch_end


def _index_next_batch(engine: Engine) -> bool:
    """Index one batch of deletes and new entries; return whether there was any."""
    with query_name_scope("omnigent.database.update_trigram_index"), engine.connect() as conn:
        conn.exec_driver_sql("BEGIN IMMEDIATE")
        deleted_through = _batch_end(
            conn.exec_driver_sql(
                f"SELECT row_id, length(search_text) FROM {_DELETES_TABLE} "
                f"ORDER BY row_id LIMIT {_INDEX_BATCH_ROWS}"
            ).all()
        )
        if deleted_through is not None:
            conn.exec_driver_sql(
                f"INSERT INTO {TRIGRAM_FTS_TABLE}({TRIGRAM_FTS_TABLE}, rowid, search_text) "
                f"SELECT 'delete', row_id, search_text FROM {_DELETES_TABLE} WHERE row_id <= ?",
                (deleted_through,),
            )
            conn.exec_driver_sql(
                f"DELETE FROM {_DELETES_TABLE} WHERE row_id <= ?", (deleted_through,)
            )

        indexed_through = conn.exec_driver_sql(
            f"SELECT indexed_through FROM {_STATE_TABLE} WHERE id = 1"
        ).scalar_one()
        indexed_end = _batch_end(
            conn.exec_driver_sql(
                f"SELECT {TRIGRAM_ROWS_TABLE}.id, "
                "COALESCE(length(conversation_items.search_text), 0) "
                f"FROM {TRIGRAM_ROWS_TABLE} LEFT {_JOIN_INDEXED_ITEMS} "
                f"WHERE {TRIGRAM_ROWS_TABLE}.id > ? "
                f"ORDER BY {TRIGRAM_ROWS_TABLE}.id LIMIT {_INDEX_BATCH_ROWS}",
                (indexed_through,),
            ).all()
        )
        if indexed_end is not None:
            conn.exec_driver_sql(
                f"INSERT INTO {TRIGRAM_FTS_TABLE}(rowid, search_text) "
                f"SELECT {TRIGRAM_ROWS_TABLE}.id, conversation_items.search_text "
                f"FROM {TRIGRAM_ROWS_TABLE} {_JOIN_INDEXED_ITEMS} "
                f"WHERE {TRIGRAM_ROWS_TABLE}.id > ? AND {TRIGRAM_ROWS_TABLE}.id <= ?",
                (indexed_through, indexed_end),
            )
            conn.exec_driver_sql(
                f"UPDATE {_STATE_TABLE} SET indexed_through = ? WHERE id = 1", (indexed_end,)
            )
        conn.commit()
    return deleted_through is not None or indexed_end is not None
