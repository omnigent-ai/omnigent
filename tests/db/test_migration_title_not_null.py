"""Tests for the conversations.title NOT NULL migration (s1a2b3c4d5e6).

Verifies that after upgrade the column is NOT NULL with a server default
of empty string, that NULL rows are back-filled, and that downgrade
restores nullable and converts empty strings back to NULL.
"""

from __future__ import annotations

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


@pytest.fixture
def db_engine(tmp_path: Path) -> Iterator[Engine]:
    """Fresh SQLite database with the full migration chain applied (including s1a2b3c4d5e6)."""
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_title_column_is_not_nullable(db_engine: Engine) -> None:
    """After upgrade, conversations.title must be NOT NULL."""
    cols = sa.inspect(db_engine).get_columns("conversations")
    title_cols = [c for c in cols if c["name"] == "title"]
    assert len(title_cols) == 1, "Expected exactly one 'title' column on conversations"
    assert not title_cols[0]["nullable"], (
        "conversations.title must be NOT NULL after the migration"
    )


def test_title_defaults_empty_string_on_insert(db_engine: Engine) -> None:
    """An insert omitting title lands as '' (the server_default)."""
    with db_engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, kind, root_conversation_id) "
                "VALUES (:id, :ts, :ts, 1, :id)"
            ),
            {"id": "conv_title_default", "ts": 1700000000},
        )
        conn.commit()
        value = conn.execute(
            sa.text("SELECT title FROM conversations WHERE id = :id"),
            {"id": "conv_title_default"},
        ).scalar_one()
    assert value == "", f"Expected title to default to '' (empty string); got {value!r}."


def test_downgrade_restores_nullable_and_nullifies_empty_titles(tmp_path: Path) -> None:
    """
    Downgrade to r1a2b3c4d5e6 makes title nullable and converts '' back to NULL.
    """
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    # At head: insert a row with no title (will be ''). kind=1 is the
    # "default" int code (conversations.kind is a SMALLINT at head).
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, kind, root_conversation_id, title) "
                "VALUES (:id, :ts, :ts, 1, :id, '')"
            ),
            {"id": "conv_downgrade_test", "ts": 1700000001},
        )
        conn.commit()

    # Also insert a row with a real title to verify it is not affected.
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, kind, root_conversation_id, title) "
                "VALUES (:id, :ts, :ts, 1, :id, :title)"
            ),
            {"id": "conv_titled", "ts": 1700000002, "title": "My Session"},
        )
        conn.commit()

    # Downgrade to r1a2b3c4d5e6 (below the title migration; passes back through
    # the enums→SMALLINT downgrade that restores kind to a string).
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "r1a2b3c4d5e6")

    inspector = sa.inspect(engine)
    title_col = next(c for c in inspector.get_columns("conversations") if c["name"] == "title")
    assert title_col["nullable"], "conversations.title must be nullable after downgrade"

    # Empty-string title should have been converted back to NULL.
    with engine.connect() as conn:
        value = conn.execute(
            sa.text("SELECT title FROM conversations WHERE id = :id"),
            {"id": "conv_downgrade_test"},
        ).scalar_one_or_none()
    assert value is None, (
        f"Expected empty-string title to be restored to NULL after downgrade; got {value!r}"
    )

    # A real title should be unchanged.
    with engine.connect() as conn:
        value = conn.execute(
            sa.text("SELECT title FROM conversations WHERE id = :id"),
            {"id": "conv_titled"},
        ).scalar_one_or_none()
    assert value == "My Session", f"Real title should be unchanged after downgrade; got {value!r}"

    engine.dispose()
    clear_engine_cache()
