"""Tests for the usage-totals migration (cb696af2aa55).

The migration and the ORM models add a nullable, bounded binary column to
``omnigent_conversation_metadata`` and ``projects`` with no database default;
existing rows keep their values and read ``NULL``, and downgrading drops both.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.dialects import mysql

from omnigent.db.compression import CompressedText
from omnigent.db.db_models import (
    USAGE_TOTALS_MAX_STORED_BYTES,
    SqlConversationMetadata,
    SqlProject,
)
from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PREVIOUS = "5ab01aaca219"
_REVISION = "cb696af2aa55"
_ROW_ID = bytes.fromhex("0123456789abcdef0123456789abcdef")
_COLUMNS = (
    ("omnigent_conversation_metadata", "subtree_usage"),
    ("projects", "project_usage"),
)


def _migrate(uri: str, engine: sa.Engine, step: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, step)(config, revision)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[str, sa.Engine]]:
    uri = f"sqlite:///{tmp_path / 'usage_totals.db'}"
    engine = sa.create_engine(uri)
    try:
        yield uri, engine
    finally:
        engine.dispose()
        clear_engine_cache()


@pytest.mark.parametrize(
    "attribute", [SqlConversationMetadata.subtree_usage, SqlProject.project_usage]
)
def test_model_declares_a_deferred_bounded_column(attribute: sa.orm.InstrumentedAttribute) -> None:
    column = attribute.property.columns[0]
    assert column.nullable
    assert column.server_default is None
    assert attribute.property.deferred
    assert isinstance(column.type, CompressedText)
    assert column.type.max_stored_bytes == USAGE_TOTALS_MAX_STORED_BYTES
    assert column.type.compile(dialect=mysql.dialect()) == (
        f"VARBINARY({USAGE_TOTALS_MAX_STORED_BYTES})"
    )


def test_upgrade_adds_the_columns_and_keeps_rows(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
    _migrate(uri, engine, "upgrade", _PREVIOUS)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                "INSERT INTO omnigent_conversation_metadata (workspace_id, id, kind, git_branch) "
                "VALUES (0, :id, 1, 'main')"
            ),
            {"id": _ROW_ID},
        )
        conn.execute(
            sa.text(
                "INSERT INTO projects (workspace_id, id, name, created_at) "
                "VALUES (0, :id, 'Factory', 1)"
            ),
            {"id": _ROW_ID},
        )

    _migrate(uri, engine, "upgrade", _REVISION)

    inspector = sa.inspect(engine)
    for table, name in _COLUMNS:
        column = {column["name"]: column for column in inspector.get_columns(table)}[name]
        assert column["nullable"] is True
        assert column["default"] is None
        assert isinstance(column["type"], sa.LargeBinary)
    with engine.connect() as conn:
        assert tuple(
            conn.execute(
                sa.text("SELECT git_branch, subtree_usage FROM omnigent_conversation_metadata")
            ).one()
        ) == ("main", None)
        assert tuple(conn.execute(sa.text("SELECT name, project_usage FROM projects")).one()) == (
            "Factory",
            None,
        )


def test_downgrade_drops_the_columns(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
    _migrate(uri, engine, "upgrade", _REVISION)

    _migrate(uri, engine, "downgrade", _PREVIOUS)

    inspector = sa.inspect(engine)
    for table, name in _COLUMNS:
        assert name not in {column["name"] for column in inspector.get_columns(table)}
