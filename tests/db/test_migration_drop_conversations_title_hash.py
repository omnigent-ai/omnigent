"""Tests for migration ``72e6dceae14f`` (drop title_hash + the unique index).

Per-parent child-title uniqueness moved into application code
(``create_conversation`` seeks the parent's children before inserting), so the
UNIQUE index ``ix_conversations_parent_title_unique`` and the ``title_hash``
column that keyed it are removed. These assert the head shape (both gone,
``idx_conversations_parent`` preserved with its DESC ordering) and that the
downgrade restores the column, its backfill, and the index.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.engine import Engine

from omnigent.db.utils import (
    _build_alembic_config,
    clear_engine_cache,
    get_or_create_engine,
)

_REVISION = "72e6dceae14f"
_PRE_REVISION = "b3c1a2d4e5f6"
_UNIQUE_INDEX = "ix_conversations_parent_title_unique"
_PARENT_INDEX = "idx_conversations_parent"


def _title_hash(title: str) -> bytes:
    return hashlib.sha256(title.encode("utf-8")).digest()[:16]


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite DB with the full alembic chain applied (at head)."""
    engine = get_or_create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_title_hash_column_gone_at_head(db_engine: Engine) -> None:
    """``conversations.title_hash`` is absent once the migration has applied."""
    cols = {c["name"] for c in sa.inspect(db_engine).get_columns("conversations")}
    assert "title_hash" not in cols, f"title_hash should be dropped at head: {cols}"


def test_unique_index_gone_at_head(db_engine: Engine) -> None:
    """The per-parent title UNIQUE index is dropped; uniqueness is app-level."""
    idx = {i["name"] for i in sa.inspect(db_engine).get_indexes("conversations")}
    assert _UNIQUE_INDEX not in idx, f"{_UNIQUE_INDEX} should be gone: {sorted(idx)}"


def test_parent_index_preserved_at_head(db_engine: Engine) -> None:
    """``idx_conversations_parent`` survives the column drop (SQLite rebuild)."""
    idx = {i["name"]: i for i in sa.inspect(db_engine).get_indexes("conversations")}
    assert _PARENT_INDEX in idx, f"{_PARENT_INDEX} missing: {sorted(idx)}"
    assert idx[_PARENT_INDEX]["column_names"] == [
        "workspace_id",
        "parent_conversation_id",
        "created_at",
        "id",
    ], idx[_PARENT_INDEX]["column_names"]


def test_parent_index_keeps_desc_ordering(tmp_path: Path) -> None:
    """The rebuilt ``idx_conversations_parent`` keeps its ``DESC`` sort order.

    SQLAlchemy's SQLite reflection doesn't surface column sort direction, so
    assert against the raw DDL in ``sqlite_master`` instead.
    """
    uri = f"sqlite:///{tmp_path / 'ddl.db'}"
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    try:
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, _REVISION)
        with engine.connect() as conn:
            ddl = conn.execute(
                sa.text("SELECT sql FROM sqlite_master WHERE type='index' AND name = :n"),
                {"n": _PARENT_INDEX},
            ).scalar_one()
        assert "created_at DESC" in ddl and "id DESC" in ddl, ddl
    finally:
        engine.dispose()
        clear_engine_cache()


def test_downgrade_restores_hash_index_and_backfills(tmp_path: Path) -> None:
    """Downgrade re-adds ``title_hash`` (back-filled) and the UNIQUE index."""
    uri = f"sqlite:///{tmp_path / 'roundtrip.db'}"
    cfg = _build_alembic_config(uri)
    engine = sa.create_engine(uri)
    conv_id = "94c349190e241f85a984b3df8f129696"
    title = "coder:legacy session"
    try:
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.upgrade(cfg, _REVISION)
            # Seed a row at head (no title_hash) to prove the backfill.
            conn.execute(
                sa.text(
                    "INSERT INTO conversations "
                    "(workspace_id, id, created_at, updated_at, root_conversation_id, title) "
                    "VALUES (0, :id, 1, 1, :id, :t)"
                ),
                {"id": conv_id, "t": title},
            )
        with engine.begin() as conn:
            cfg.attributes["connection"] = conn
            command.downgrade(cfg, _PRE_REVISION)

        insp = sa.inspect(engine)
        cols = {c["name"] for c in insp.get_columns("conversations")}
        assert "title_hash" in cols, "downgrade must restore title_hash."
        idx = {i["name"]: i for i in insp.get_indexes("conversations")}
        assert idx[_UNIQUE_INDEX]["unique"], f"{_UNIQUE_INDEX} must be UNIQUE again."
        assert idx[_UNIQUE_INDEX]["column_names"] == [
            "workspace_id",
            "parent_conversation_id",
            "title_hash",
        ], idx[_UNIQUE_INDEX]["column_names"]
        with engine.connect() as conn:
            stored = conn.execute(
                sa.text("SELECT title_hash FROM conversations WHERE id = :id"),
                {"id": conv_id},
            ).scalar_one()
        assert bytes(stored) == _title_hash(title), "backfilled hash != sha256(title)[:16]"
    finally:
        engine.dispose()
        clear_engine_cache()
