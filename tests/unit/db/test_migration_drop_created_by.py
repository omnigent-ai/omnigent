"""Tests for the created_by migration chain on conversation_items.

The column is added by e1c4a7b2f309, dropped by b9c0d1e2f3a4, and re-added
by i1a2b3c4d5e6. All three steps are exercised by upgrading to head, which
leaves the column present.
"""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command as alembic_command
from alembic.config import Config

from omnigent.db.utils import clear_engine_cache, get_or_create_engine


def _column_names(conn: sa.Connection, table: str) -> list[str]:
    return [c["name"] for c in sa.inspect(conn).get_columns(table)]


def _alembic_cfg(uri: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "omnigent/db/migrations")
    cfg.set_main_option("sqlalchemy.url", uri)
    return cfg


def test_created_by_present_after_full_migration(tmp_path: Path) -> None:
    """
    A full upgrade to head leaves created_by present on conversation_items.

    e1c4a7b2f309 adds the column, b9c0d1e2f3a4 drops it, and i1a2b3c4d5e6
    re-adds it. After running all three the column must exist so the ORM
    (which declares the field) can read and write it.
    """
    db_path = tmp_path / "test.db"
    engine = get_or_create_engine(f"sqlite:///{db_path}")
    try:
        with engine.connect() as conn:
            cols = _column_names(conn, "conversation_items")
        assert "created_by" in cols, (
            "conversation_items.created_by should exist after upgrading to "
            "head — i1a2b3c4d5e6 re-adds it after b9c0d1e2f3a4's drop."
        )
    finally:
        clear_engine_cache()


def test_created_by_present_after_add_migration(tmp_path: Path) -> None:
    """
    Upgrading to e1c4a7b2f309 (the add migration) creates the column.

    This pins the add step so that b9c0d1e2f3a4's drop is meaningful —
    if the add never ran, the drop would be a no-op and the head test
    would pass vacuously.
    """
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = sa.create_engine(uri)
    alembic_command.upgrade(_alembic_cfg(uri), "e1c4a7b2f309")
    with engine.connect() as conn:
        cols = _column_names(conn, "conversation_items")
    assert "created_by" in cols, (
        "e1c4a7b2f309 must add created_by to conversation_items — "
        "without this the drop migration is a no-op."
    )
    engine.dispose()


def test_drop_migration_removes_column(tmp_path: Path) -> None:
    """
    Upgrading from e1c4a7b2f309 to b9c0d1e2f3a4 removes the column.

    Directly exercises the drop path for users who had already applied
    the add migration and are now upgrading.
    """
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = sa.create_engine(uri)

    alembic_command.upgrade(_alembic_cfg(uri), "e1c4a7b2f309")
    with engine.connect() as conn:
        assert "created_by" in _column_names(conn, "conversation_items")

    alembic_command.upgrade(_alembic_cfg(uri), "b9c0d1e2f3a4")
    with engine.connect() as conn:
        cols = _column_names(conn, "conversation_items")
    assert "created_by" not in cols, "b9c0d1e2f3a4 must drop created_by from conversation_items."
    engine.dispose()


def test_readd_migration_restores_column(tmp_path: Path) -> None:
    """
    Upgrading from b9c0d1e2f3a4 to i1a2b3c4d5e6 re-adds the column.

    Mirrors a deployed database that already applied the drop: the re-add
    step must bring created_by back so the restored attribution feature
    has its storage.
    """
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = sa.create_engine(uri)

    alembic_command.upgrade(_alembic_cfg(uri), "b9c0d1e2f3a4")
    with engine.connect() as conn:
        assert "created_by" not in _column_names(conn, "conversation_items")

    alembic_command.upgrade(_alembic_cfg(uri), "i1a2b3c4d5e6")
    with engine.connect() as conn:
        cols = _column_names(conn, "conversation_items")
    assert "created_by" in cols, "i1a2b3c4d5e6 must re-add created_by to conversation_items."
    engine.dispose()
