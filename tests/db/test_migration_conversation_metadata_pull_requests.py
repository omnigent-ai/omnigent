"""Tests for the conversation metadata ``pull_requests`` migration (d3f7a1c9e2b4).

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

_PREVIOUS = "c8e4f2a6b901"
_REVISION = "d3f7a1c9e2b4"
_MERGE_REVISION = "b2e4f6a8c013"
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
    column = SqlConversationMetadata.__table__.c.pull_requests
    assert column.nullable
    assert isinstance(column.type, CompressedText)
    assert column.server_default is None
    assert SqlConversationMetadata.pull_requests.property.deferred


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
    assert columns["pull_requests"]["nullable"] is True
    assert columns["pull_requests"]["default"] is None
    assert isinstance(columns["pull_requests"]["type"], sa.LargeBinary)
    with engine.connect() as conn:
        row = conn.execute(sa.text(f"SELECT git_branch, pull_requests FROM {_TABLE}")).one()
    assert tuple(row) == ("main", None)


def test_downgrade_drops_the_column(database: tuple[str, sa.Engine]) -> None:
    uri, engine = database
    _migrate(uri, engine, "upgrade", _REVISION)

    _migrate(uri, engine, "downgrade", _PREVIOUS)

    assert "pull_requests" not in {
        column["name"] for column in sa.inspect(engine).get_columns(_TABLE)
    }


@pytest.mark.parametrize("starting_revision", ["base", "9e82957bd115", _REVISION])
def test_merge_reconciles_both_schema_branches(
    database: tuple[str, sa.Engine], starting_revision: str
) -> None:
    """Fresh databases and either deployed branch gain both schemas and one head."""
    uri, engine = database
    _migrate(uri, engine, "upgrade", starting_revision)
    _migrate(uri, engine, "upgrade", _MERGE_REVISION)

    assert "pull_requests" in {column["name"] for column in sa.inspect(engine).get_columns(_TABLE)}
    tasks = sa.Table("scheduled_tasks", sa.MetaData(), autoload_with=engine)
    with engine.begin() as conn:
        conn.execute(
            tasks.insert().values(
                id=_SESSION_ID,
                name="ended",
                prompt=b"p",
                rrule="FREQ=DAILY;COUNT=2",
                agent_id=b"g" * 16,
                timezone="UTC",
                created_at=1,
                state=4,
            )
        )
        assert (
            conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            == _MERGE_REVISION
        )

    _migrate(uri, engine, "downgrade", _REVISION)
    with engine.connect() as conn:
        assert set(conn.execute(sa.text("SELECT version_num FROM alembic_version")).scalars()) == {
            "9e82957bd115",
            _REVISION,
        }
        assert conn.execute(sa.select(tasks.c.state)).scalar_one() == 4
    _migrate(uri, engine, "upgrade", _MERGE_REVISION)
