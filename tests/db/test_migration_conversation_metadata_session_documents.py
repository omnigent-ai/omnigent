"""Tests for the conversation metadata ``session_documents`` migration (c6a8e2f401bd).

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
_REVISION = "c6a8e2f401bd"
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
    column = SqlConversationMetadata.__table__.c.session_documents
    assert column.nullable
    assert isinstance(column.type, CompressedText)
    assert column.server_default is None
    assert SqlConversationMetadata.session_documents.property.deferred


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

    columns = {column["name"]: column for column in sa.inspect(engine).get_columns(_TABLE)}
    assert columns["session_documents"]["nullable"] is True
    assert columns["session_documents"]["default"] is None
    assert isinstance(columns["session_documents"]["type"], sa.LargeBinary)
    with engine.connect() as conn:
        row = conn.execute(sa.text(f"SELECT git_branch, session_documents FROM {_TABLE}")).one()
    assert tuple(row) == ("main", None)


def test_downgrade_drops_the_column(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
    _migrate(uri, engine, "upgrade", _REVISION)

    _migrate(uri, engine, "downgrade", _PREVIOUS)

    assert "session_documents" not in {
        column["name"] for column in sa.inspect(engine).get_columns(_TABLE)
    }
