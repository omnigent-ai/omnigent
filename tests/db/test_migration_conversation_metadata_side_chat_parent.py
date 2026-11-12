"""Tests for the conversation metadata ``side_chat_parent_id`` migration (e5b2c8d4f1a7).

The migration and the ORM model add a nullable uuid column with no default,
plus a (workspace, side chat parent, id) index; existing rows read ``NULL``,
and downgrading drops both.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import SqlConversationMetadata, Uuid16
from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PREVIOUS = "d0a1f879a01d"
_REVISION = "e5b2c8d4f1a7"
_TABLE = "omnigent_conversation_metadata"
_INDEX = "ix_conversation_metadata_side_chat_parent_id"
_SESSION_ID = bytes.fromhex("0123456789abcdef0123456789abcdef")


def _migrate(uri: str, engine: sa.Engine, step: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as conn:
        config.attributes["connection"] = conn
        getattr(command, step)(config, revision)


def _indexes(engine: sa.Engine) -> dict[str, list[str]]:
    return {
        index["name"]: index["column_names"] for index in sa.inspect(engine).get_indexes(_TABLE)
    }


@pytest.fixture
def database(tmp_path: Path) -> Iterator[tuple[str, sa.Engine]]:
    uri = f"sqlite:///{tmp_path / 'metadata.db'}"
    engine = sa.create_engine(uri)
    try:
        yield uri, engine
    finally:
        engine.dispose()
        clear_engine_cache()


def test_model_declares_a_nullable_uuid_column() -> None:
    table = SqlConversationMetadata.__table__
    column = table.c.side_chat_parent_id
    assert column.nullable
    assert isinstance(column.type, Uuid16)
    assert column.server_default is None
    index = next(index for index in table.indexes if index.name == _INDEX)
    assert [indexed.name for indexed in index.columns] == [
        "workspace_id",
        "side_chat_parent_id",
        "id",
    ]


def test_upgrade_adds_the_indexed_column_and_keeps_rows(
    database: tuple[str, sa.Engine],
) -> None:
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
    assert columns["side_chat_parent_id"]["nullable"] is True
    assert columns["side_chat_parent_id"]["default"] is None
    assert _indexes(engine)[_INDEX] == ["workspace_id", "side_chat_parent_id", "id"]
    with engine.connect() as conn:
        row = conn.execute(sa.text(f"SELECT git_branch, side_chat_parent_id FROM {_TABLE}")).one()
    assert tuple(row) == ("main", None)


def test_downgrade_drops_the_column_and_index(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
    _migrate(uri, engine, "upgrade", _REVISION)

    _migrate(uri, engine, "downgrade", _PREVIOUS)

    assert "side_chat_parent_id" not in {
        column["name"] for column in sa.inspect(engine).get_columns(_TABLE)
    }
    assert _INDEX not in _indexes(engine)
