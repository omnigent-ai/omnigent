"""Tests for the conversations.title VARCHAR(768) migration (w1a2b3c4d5e6).

Verifies that after upgrade the column is VARCHAR(768) and NOT NULL with a
server_default of empty string, and that downgrade restores it to Text.
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
    """Fresh SQLite database with the full migration chain applied (including w1a2b3c4d5e6)."""
    db_path = tmp_path / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    try:
        yield engine
    finally:
        clear_engine_cache()


def test_title_column_is_varchar768(db_engine: Engine) -> None:
    """After upgrade, conversations.title must be VARCHAR(768) and NOT NULL."""
    cols = sa.inspect(db_engine).get_columns("conversations")
    title_cols = [c for c in cols if c["name"] == "title"]
    assert len(title_cols) == 1, "Expected exactly one 'title' column on conversations"
    col = title_cols[0]
    assert not col["nullable"], "conversations.title must be NOT NULL after the migration"
    col_type = col["type"]
    assert isinstance(col_type, sa.String), (
        f"conversations.title must be a String type; got {type(col_type)}"
    )
    assert col_type.length == 768, (
        f"conversations.title must be VARCHAR(768); got VARCHAR({col_type.length})"
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
            {"id": "conv_varchar768_default", "ts": 1700000000},
        )
        conn.commit()
        value = conn.execute(
            sa.text("SELECT title FROM conversations WHERE id = :id"),
            {"id": "conv_varchar768_default"},
        ).scalar_one()
    assert value == "", f"Expected title to default to '' (empty string); got {value!r}."


def test_existing_title_survives_upgrade(db_engine: Engine) -> None:
    """Existing title data must survive the column type change."""
    long_title = "A" * 768
    with db_engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, kind, root_conversation_id, title) "
                "VALUES (:id, :ts, :ts, 1, :id, :title)"
            ),
            {"id": "conv_long_title", "ts": 1700000001, "title": long_title},
        )
        conn.commit()
        value = conn.execute(
            sa.text("SELECT title FROM conversations WHERE id = :id"),
            {"id": "conv_long_title"},
        ).scalar_one()
    assert value == long_title, f"Long title should survive upgrade; got {value!r}"


def test_downgrade_restores_text_type(tmp_path: Path) -> None:
    """Downgrade to v1a2b3c4d5e6 restores conversations.title to Text."""
    db_path = tmp_path / "downgrade.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)

    # Insert a row with a title at head.
    with engine.connect() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO conversations "
                "(id, created_at, updated_at, kind, root_conversation_id, title) "
                "VALUES (:id, :ts, :ts, 1, :id, :title)"
            ),
            {"id": "conv_downgrade_varchar", "ts": 1700000002, "title": "My Session"},
        )
        conn.commit()

    # Downgrade one step to v1a2b3c4d5e6.
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        command.downgrade(config, "v1a2b3c4d5e6")

    inspector = sa.inspect(engine)
    title_col = next(c for c in inspector.get_columns("conversations") if c["name"] == "title")
    col_type = title_col["type"]
    # After downgrade the column should be Text (not a length-bounded String).
    assert isinstance(col_type, sa.Text), (
        f"conversations.title must be Text after downgrade; got {type(col_type)}"
    )
    assert not title_col["nullable"], "conversations.title must still be NOT NULL after downgrade"

    # Data must survive the round-trip.
    with engine.connect() as conn:
        value = conn.execute(
            sa.text("SELECT title FROM conversations WHERE id = :id"),
            {"id": "conv_downgrade_varchar"},
        ).scalar_one_or_none()
    assert value == "My Session", f"Title should survive downgrade; got {value!r}"

    engine.dispose()
    clear_engine_cache()
