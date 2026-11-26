"""Tests for the conversation metadata ``context_breakdown`` migration (zb1c2d3e4f5a).

The migration and the ORM model both add a nullable binary column with no
database default; existing rows keep their values and read ``NULL``, and
downgrading drops the column.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.compression import CompressedText
from omnigent.db.db_models import SqlConversationMetadata
from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PREVIOUS = "bf30d7cbda61"
_REVISION = "zb1c2d3e4f5a"
_TABLE = "omnigent_conversation_metadata"
_SESSION_ID = bytes.fromhex("0123456789abcdef0123456789abcdef")


def _migrate(uri: str, engine: sa.Engine, step: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, step)(config, revision)


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[str, sa.Engine]]:
    uri = f"sqlite:///{tmp_path / 'metadata.db'}"
    engine = sa.create_engine(uri)
    try:
        yield uri, engine
    finally:
        engine.dispose()
        clear_engine_cache()


def test_model_declares_a_deferred_compressed_column() -> None:
    column = SqlConversationMetadata.__table__.c.context_breakdown
    assert column.nullable
    assert isinstance(column.type, CompressedText)
    assert column.server_default is None
    assert SqlConversationMetadata.context_breakdown.property.deferred


def test_upgrade_adds_the_column_and_keeps_rows(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
    _migrate(uri, engine, "upgrade", _PREVIOUS)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                f"INSERT INTO {_TABLE} (workspace_id, id, kind, git_branch) "
                "VALUES (0, :id, 1, 'main')"
            ),
            {"id": _SESSION_ID},
        )

    _migrate(uri, engine, "upgrade", _REVISION)
    # Re-running against a database that already has the column is a no-op.
    _migrate(uri, engine, "stamp", _PREVIOUS)
    _migrate(uri, engine, "upgrade", _REVISION)

    columns = {column["name"]: column for column in sa.inspect(engine).get_columns(_TABLE)}
    assert columns["context_breakdown"]["nullable"] is True
    assert columns["context_breakdown"]["default"] is None
    assert isinstance(columns["context_breakdown"]["type"], sa.LargeBinary)
    with engine.connect() as conn:
        row = conn.execute(sa.text(f"SELECT git_branch, context_breakdown FROM {_TABLE}")).one()
    assert tuple(row) == ("main", None)


def test_downgrade_drops_the_column(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
    _migrate(uri, engine, "upgrade", _REVISION)

    _migrate(uri, engine, "downgrade", _PREVIOUS)

    assert "context_breakdown" not in {
        column["name"] for column in sa.inspect(engine).get_columns(_TABLE)
    }
