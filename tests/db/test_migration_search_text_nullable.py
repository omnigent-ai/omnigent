"""Nullable search_text migration: upgrade keeps rows and FTS, downgrade backfills by page."""

from __future__ import annotations

from collections.abc import Iterable
from importlib import import_module
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine

from omnigent.db.utils import _build_alembic_config, clear_engine_cache, ensure_fts_table

# Revision ids bounding the migration under test.
_PRIOR = "mm1a2b3c4d5e"
_THIS = "nn1a2b3c4d5e"
_MIGRATION = "omnigent.db.migrations.versions.nn1a2b3c4d5e_conversation_items_search_text_nullable"
_CONVERSATION_ID = b"\x01" * 16
_BACKFILL_UPDATE = "UPDATE conversation_items SET search_text = ''"


def _engine_at(uri: str, revision: str) -> Engine:
    """
    :param uri: SQLAlchemy database URI, e.g. ``"sqlite:///tmp/test.db"``.
    :param revision: Alembic revision to upgrade the fresh database to.
    :returns: Engine over a database migrated exactly to *revision*.
    """
    engine = sa.create_engine(uri)
    _migrate(engine, revision, downgrade=False)
    return engine


def _migrate(engine: Engine, revision: str, downgrade: bool) -> None:
    """Run an Alembic upgrade or downgrade to *revision* on *engine*."""
    cfg = _build_alembic_config(str(engine.url))
    # A plain connection, not ``engine.begin()``: the migration commits its own pages.
    with engine.connect() as conn:
        cfg.attributes["connection"] = conn
        if downgrade:
            command.downgrade(cfg, revision)
        else:
            command.upgrade(cfg, revision)
        conn.commit()
    # Drop pooled connections so later reflection sees the migrated schema.
    engine.dispose()


def _item_id(position: int) -> bytes:
    return position.to_bytes(16)


def _insert_item(engine: Engine, position: int, search_text: str | None) -> None:
    """Insert one conversation_items row, omitting search_text when None."""
    columns = "conversation_id, id, response_id, created_at, status, position, type, data"
    params: dict[str, object] = {
        "cid": _CONVERSATION_ID,
        "iid": _item_id(position),
        "rid": "resp_1",
        "created": 1,
        "status": 1,
        "pos": position,
        "type": 1,
        "data": "{}",
    }
    values = ":cid, :iid, :rid, :created, :status, :pos, :type, :data"
    if search_text is not None:
        columns += ", search_text"
        values += ", :st"
        params["st"] = search_text
    with engine.begin() as conn:
        conn.execute(
            sa.text(f"INSERT INTO conversation_items ({columns}) VALUES ({values})"),
            params,
        )


def _insert_null_items(engine: Engine, positions: Iterable[int]) -> None:
    """Bulk-insert rows with a NULL search_text (requires the nullable schema)."""
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversation_items (conversation_id, id, response_id, created_at, "
                "status, position, type, data, search_text) "
                "VALUES (:cid, :iid, 'resp_1', 1, 1, :pos, 1, '{}', NULL)"
            ),
            [{"cid": _CONVERSATION_ID, "iid": _item_id(p), "pos": p} for p in positions],
        )


def _insert_fts_row(engine: Engine, position: int, search_text: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversation_items_fts (item_id, conversation_id, search_text) "
                "VALUES (:iid, :cid, :st)"
            ),
            {"iid": _item_id(position), "cid": _CONVERSATION_ID, "st": search_text},
        )


def _fts_matches(engine: Engine, query: str) -> int:
    with engine.begin() as conn:
        return conn.execute(
            sa.text("SELECT count(*) FROM conversation_items_fts WHERE search_text MATCH :q"),
            {"q": query},
        ).scalar_one()


def _stored_search_texts(engine: Engine) -> list[str | None]:
    """:returns: Every stored ``search_text``, ordered by item position."""
    with engine.begin() as conn:
        rows = conn.execute(
            sa.text("SELECT search_text FROM conversation_items ORDER BY position")
        )
        return [row.search_text for row in rows]


def _search_text_nullable(engine: Engine) -> bool:
    """:returns: The reflected nullability of ``conversation_items.search_text``."""
    columns = sa.inspect(engine).get_columns("conversation_items")
    return next(c["nullable"] for c in columns if c["name"] == "search_text")


@pytest.fixture
def db_file(tmp_path: Path):
    """SQLite URI for a per-test database, with the engine cache cleared."""
    try:
        yield f"sqlite:///{tmp_path / 'test.db'}"
    finally:
        clear_engine_cache()


def test_upgrade_accepts_insert_without_search_text(db_file: str) -> None:
    """Post-upgrade, a column-less INSERT lands with ``search_text`` NULL.

    At the prior revision the same INSERT aborts on the NOT NULL constraint
    (the failure a search-text-less store hit on every append); pre-existing
    plaintext rows and their FTS index must survive the SQLite table rebuild.
    """
    engine = _engine_at(db_file, _PRIOR)
    ensure_fts_table(engine)
    _insert_item(engine, 2, search_text="hello body")
    _insert_fts_row(engine, 2, "hello body")
    with pytest.raises(sa.exc.IntegrityError, match="search_text"):
        _insert_item(engine, 3, search_text=None)

    _migrate(engine, _THIS, downgrade=False)

    _insert_item(engine, 4, search_text=None)
    assert _stored_search_texts(engine) == ["hello body", None]
    assert _search_text_nullable(engine) is True
    assert _fts_matches(engine, "hello") == 1
    engine.dispose()


def test_downgrade_commits_each_page_and_resumes(db_file: str) -> None:
    """An interrupted downgrade keeps the page it committed and finishes when rerun.

    Item ids follow position order, so the first page holds positions below the
    batch size. Failing the first UPDATE of the second page must leave the first
    page's backfill committed, the rest NULL, and the column still nullable.
    """
    batch = import_module(_MIGRATION)._BACKFILL_BATCH
    engine = _engine_at(db_file, _THIS)
    _insert_item(engine, 0, search_text="kept text")
    _insert_null_items(engine, range(1, batch + 3))
    updates = 0

    def fail_in_second_page(conn, cursor, statement, parameters, context, executemany):
        nonlocal updates
        if statement.startswith(_BACKFILL_UPDATE):
            updates += 1
            if updates == batch:
                raise RuntimeError("interrupted second page")

    sa.event.listen(engine, "before_cursor_execute", fail_in_second_page)
    try:
        with pytest.raises(RuntimeError, match="interrupted second page"):
            _migrate(engine, _PRIOR, downgrade=True)
    finally:
        sa.event.remove(engine, "before_cursor_execute", fail_in_second_page)

    texts = _stored_search_texts(engine)
    assert texts[:batch] == ["kept text"] + [""] * (batch - 1)
    assert texts[batch:] == [None] * 3
    assert _search_text_nullable(engine) is True

    _migrate(engine, _PRIOR, downgrade=True)

    assert _stored_search_texts(engine) == ["kept text"] + [""] * (batch + 2)
    assert _search_text_nullable(engine) is False
    engine.dispose()
