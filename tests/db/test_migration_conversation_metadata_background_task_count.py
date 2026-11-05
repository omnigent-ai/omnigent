"""Tests for the conversation metadata ``background_task_count`` migration (a3f8b3a5902c).

The migration and the ORM model both add a nullable Integer column with no
database default; existing rows keep their values and read ``NULL``, and
downgrading drops the column while keeping the rows.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command

from omnigent.db.db_models import SqlConversationMetadata
from omnigent.db.utils import _build_alembic_config, clear_engine_cache

_PREVIOUS = "c7cd88f24c1c"
_REVISION = "a3f8b3a5902c"
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


def test_model_declares_a_nullable_integer_column() -> None:
    column = SqlConversationMetadata.__table__.c.background_task_count
    assert column.nullable
    assert isinstance(column.type, sa.Integer)
    assert column.server_default is None


def test_upgrade_adds_the_column_and_downgrade_drops_it_keeping_rows(
    database: tuple[str, sa.Engine],
) -> None:
    uri, engine = database
    _migrate(uri, engine, "upgrade", _PREVIOUS)
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                f"INSERT INTO {_TABLE} (workspace_id, id, kind, pending_elicitation_count) "
                "VALUES (0, :id, 1, 2)"
            ),
            {"id": _SESSION_ID},
        )

    _migrate(uri, engine, "upgrade", _REVISION)

    columns = {column["name"]: column for column in sa.inspect(engine).get_columns(_TABLE)}
    assert columns["background_task_count"]["nullable"] is True
    assert columns["background_task_count"]["default"] is None
    assert isinstance(columns["background_task_count"]["type"], sa.Integer)
    with engine.connect() as conn:
        row = conn.execute(
            sa.text(f"SELECT pending_elicitation_count, background_task_count FROM {_TABLE}")
        ).one()
    assert tuple(row) == (2, None)

    _migrate(uri, engine, "downgrade", _PREVIOUS)

    assert "background_task_count" not in {
        column["name"] for column in sa.inspect(engine).get_columns(_TABLE)
    }
    with engine.connect() as conn:
        assert (
            conn.execute(sa.text(f"SELECT pending_elicitation_count FROM {_TABLE}")).scalar_one()
            == 2
        )
