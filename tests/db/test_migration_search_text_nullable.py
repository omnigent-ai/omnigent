"""Tests for the ``conversation_items.search_text`` nullable migration
(``gh1b2c3d4e5f``).

A store whose ``_item_search_text`` returns ``None`` (its item ``data`` is
opaque, so no plaintext body exists to index) omits ``search_text`` from the
batch INSERT. Against the prior NOT NULL constraint that aborted the whole
INSERT — relay persistence silently lost every item on such a store — so the
upgrade must accept column-less inserts (stored NULL) while keeping existing
plaintext rows intact, and the downgrade must backfill NULL to ``''`` before
restoring NOT NULL.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine

from omnigent.db.utils import _build_alembic_config, clear_engine_cache

# Revision ids bounding the migration under test.
_PRIOR = "gg1b2c3d4e5f"
_THIS = "gh1b2c3d4e5f"


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
    with engine.begin() as conn:
        cfg.attributes["connection"] = conn
        if downgrade:
            command.downgrade(cfg, revision)
        else:
            command.upgrade(cfg, revision)
    # Drop pooled connections so later reflection sees the migrated schema.
    engine.dispose()


def _insert_item(engine: Engine, item_id: bytes, search_text: str | None) -> None:
    """Insert one conversation_items row, omitting search_text when None."""
    columns = "conversation_id, id, response_id, created_at, status, position, type, data"
    params: dict[str, object] = {
        "cid": b"\x01" * 16,
        "iid": item_id,
        "rid": "resp_1",
        "created": 1,
        "status": 1,
        "pos": int.from_bytes(item_id[-2:]),
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
    plaintext rows must survive the rebuild unchanged.
    """
    engine = _engine_at(db_file, _PRIOR)
    _insert_item(engine, b"\x02" * 16, search_text="hello body")
    with pytest.raises(sa.exc.IntegrityError, match="search_text"):
        _insert_item(engine, b"\x03" * 16, search_text=None)

    _migrate(engine, _THIS, downgrade=False)

    _insert_item(engine, b"\x04" * 16, search_text=None)
    assert _stored_search_texts(engine) == ["hello body", None]
    assert _search_text_nullable(engine) is True
    engine.dispose()


def test_downgrade_backfills_null_and_restores_not_null(db_file: str) -> None:
    """Downgrade rewrites NULL rows to ``''`` and reinstates NOT NULL."""
    engine = _engine_at(db_file, _THIS)
    _insert_item(engine, b"\x02" * 16, search_text="kept text")
    _insert_item(engine, b"\x03" * 16, search_text=None)

    _migrate(engine, _PRIOR, downgrade=True)

    assert _stored_search_texts(engine) == ["kept text", ""]
    assert _search_text_nullable(engine) is False
    engine.dispose()
