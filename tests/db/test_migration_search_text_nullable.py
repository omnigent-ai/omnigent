"""Nullable search_text migration: upgrade keeps rows and FTS, downgrade backfills by page."""

from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.cockroachdb import _crdb_server_version, _prepare_crdb_schema_transaction
from omnigent.db.utils import (
    _build_alembic_config,
    _supports_fts5,
    ensure_fts_table,
    get_or_create_engine,
)

# Revision ids bounding the migration under test.
_PRIOR = "mm1a2b3c4d5e"
_THIS = "nn1a2b3c4d5e"
_MIGRATION = "omnigent.db.migrations.versions.nn1a2b3c4d5e_conversation_items_search_text_nullable"
_CONVERSATION_ID = b"\x01" * 16
_PAGE_SELECT = "SELECT conversation_items.workspace_id"
_BACKFILL_UPDATE = "UPDATE conversation_items SET search_text="


def _migrate(engine: sa.Engine, revision: str, *, downgrade: bool = False) -> None:
    """Run an Alembic upgrade or downgrade to *revision*; the migration commits its own pages."""
    config = _build_alembic_config(engine.url.render_as_string(hide_password=False))
    with engine.connect() as connection:
        if engine.dialect.name == "cockroachdb":
            _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
        config.attributes["connection"] = connection
        (command.downgrade if downgrade else command.upgrade)(config, revision)
        connection.commit()


def _items(engine: sa.Engine) -> sa.Table:
    return sa.Table("conversation_items", sa.MetaData(), autoload_with=engine)


def _item_id(position: int) -> bytes:
    return position.to_bytes(16)


def _row(position: int) -> dict[str, object]:
    """Column values for one item; ids follow position order, so pages do too."""
    return {
        "workspace_id": 0,
        "conversation_id": _CONVERSATION_ID,
        "id": _item_id(position),
        "response_id": "resp_1",
        "created_at": 1,
        "status": 1,
        "position": position,
        "type": 1,
        "data": "{}",
    }


def _insert_item(
    engine: sa.Engine, table: sa.Table, position: int, search_text: str | None
) -> None:
    """Insert one row, omitting search_text when None as a None-returning store does."""
    values = _row(position)
    if search_text is not None:
        values["search_text"] = search_text
    with engine.begin() as conn:
        conn.execute(table.insert().values(**values))


def _insert_null_items(engine: sa.Engine, table: sa.Table, positions: Iterable[int]) -> None:
    with engine.begin() as conn:
        conn.execute(table.insert(), [{**_row(p), "search_text": None} for p in positions])


def _insert_fts_row(engine: sa.Engine, position: int, search_text: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversation_items_fts (item_id, conversation_id, search_text) "
                "VALUES (:iid, :cid, :st)"
            ),
            {"iid": _item_id(position), "cid": _CONVERSATION_ID, "st": search_text},
        )


def _fts_matches(engine: sa.Engine, query: str) -> int:
    with engine.begin() as conn:
        return conn.execute(
            sa.text("SELECT count(*) FROM conversation_items_fts WHERE search_text MATCH :q"),
            {"q": query},
        ).scalar_one()


def _stored_search_texts(engine: sa.Engine) -> list[str | None]:
    """:returns: Every stored ``search_text``, ordered by item position."""
    with engine.begin() as conn:
        rows = conn.execute(
            sa.text("SELECT search_text FROM conversation_items ORDER BY position")
        )
        return [row.search_text for row in rows]


def _search_text_nullable(engine: sa.Engine) -> bool:
    """:returns: The reflected nullability of ``conversation_items.search_text``."""
    columns = sa.inspect(engine).get_columns("conversation_items")
    return next(c["nullable"] for c in columns if c["name"] == "search_text")


def test_upgrade_accepts_insert_without_search_text(db_uri: str) -> None:
    """Post-upgrade, a column-less INSERT lands with ``search_text`` NULL.

    At the prior revision the same INSERT aborts on the NOT NULL constraint
    (the failure a search-text-less store hit on every append); pre-existing
    plaintext rows, and on SQLite their FTS index, must survive the change.
    """
    engine = get_or_create_engine(db_uri)
    _migrate(engine, _PRIOR, downgrade=True)
    table = _items(engine)
    fts = _supports_fts5(engine.dialect.name)
    if fts:
        ensure_fts_table(engine)
        _insert_fts_row(engine, 2, "hello body")
    _insert_item(engine, table, 2, search_text="hello body")
    with pytest.raises(sa.exc.DBAPIError, match="search_text"):
        _insert_item(engine, table, 3, search_text=None)

    _migrate(engine, _THIS)

    _insert_item(engine, table, 4, search_text=None)
    assert _stored_search_texts(engine) == ["hello body", None]
    assert _search_text_nullable(engine) is True
    if fts:
        assert _fts_matches(engine, "hello") == 1
    _migrate(engine, "head")


def test_downgrade_commits_each_page_and_resumes(db_uri: str) -> None:
    """An interrupted downgrade keeps the page it committed and finishes when rerun.

    The first page holds positions below the batch size. Failing the second
    page's UPDATE must leave the first page's backfill committed, the rest
    NULL, and the column still nullable; the continuation SELECT must seek the
    primary key rather than scan it.
    """
    batch = import_module(_MIGRATION)._BACKFILL_BATCH
    engine = get_or_create_engine(db_uri)
    table = _items(engine)
    _insert_item(engine, table, 0, search_text="kept text")
    _insert_null_items(engine, table, range(1, batch + 3))
    dialect = engine.dialect.name
    updates = 0
    plans: list[str] = []

    def observe(conn, cursor, statement, parameters, context, executemany):
        nonlocal updates
        if not plans and statement.startswith(_PAGE_SELECT) and "WHERE" in statement:
            if dialect == "sqlite":
                rows = conn.exec_driver_sql("EXPLAIN QUERY PLAN " + statement, parameters)
                plans.append(" | ".join(str(row[-1]) for row in rows))
            elif dialect == "mysql":
                plans.append(
                    conn.exec_driver_sql("EXPLAIN ANALYZE " + statement, parameters).scalar_one()
                )
        if statement.startswith(_BACKFILL_UPDATE):
            updates += 1
            if updates == 2:
                raise RuntimeError("interrupted second page")

    sa.event.listen(engine, "before_cursor_execute", observe)
    try:
        with pytest.raises(RuntimeError, match="interrupted second page"):
            _migrate(engine, _PRIOR, downgrade=True)
    finally:
        sa.event.remove(engine, "before_cursor_execute", observe)

    if dialect == "sqlite":
        assert plans and plans[0].startswith("SEARCH conversation_items USING")
        assert "SCAN conversation_items" not in plans[0]
    elif dialect == "mysql":
        assert plans and "Index range scan on conversation_items using PRIMARY" in plans[0]
    texts = _stored_search_texts(engine)
    assert texts[:batch] == ["kept text"] + [""] * (batch - 1)
    assert texts[batch:] == [None] * 3
    assert _search_text_nullable(engine) is True

    _migrate(engine, _PRIOR, downgrade=True)

    assert _stored_search_texts(engine) == ["kept text"] + [""] * (batch + 2)
    assert _search_text_nullable(engine) is False
    _migrate(engine, "head")
