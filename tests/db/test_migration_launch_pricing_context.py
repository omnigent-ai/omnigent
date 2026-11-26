"""Launch pricing storage is additive and preserves existing session fields."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.orm import Session

from omnigent.db.db_models import ConversationBase, OmnigentBase, SqlConversationMetadata
from omnigent.db.utils import _build_alembic_config

_PREVIOUS = "47d3e8fc3e25"
_REVISION = "c51f8d9a27b6"
_TABLE = "omnigent_conversation_metadata"
_COLUMN = "launch_pricing_context_id"
_SESSION_ID = bytes.fromhex("0123456789abcdef0123456789abcdef")


def _migrate(uri: str, engine: sa.Engine, step: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        getattr(command, step)(config, revision)


@pytest.mark.parametrize(
    ("uri", "column_type"),
    [
        ("sqlite://", "BLOB"),
        ("postgresql://localhost/database", "BYTEA"),
        ("mysql://localhost/database", "BINARY(16)"),
    ],
)
def test_launch_pricing_reference_ddl_is_additive(uri: str, column_type: str) -> None:
    output = io.StringIO()
    config = _build_alembic_config(uri)
    config.output_buffer = output
    command.upgrade(config, f"{_PREVIOUS}:{_REVISION}", sql=True)
    sql = output.getvalue()
    assert f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN} {column_type}" in sql
    assert "DROP " not in sql
    assert f"UPDATE {_TABLE}" not in sql
    assert "CREATE INDEX" not in sql


def test_metadata_bootstrap_can_downgrade_the_reserved_reference(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'bootstrap.db'}"
    engine = sa.create_engine(uri)
    try:
        ConversationBase.metadata.create_all(engine)
        OmnigentBase.metadata.create_all(engine)
        _migrate(uri, engine, "stamp", _REVISION)

        _migrate(uri, engine, "downgrade", _PREVIOUS)

        assert _COLUMN not in {column["name"] for column in sa.inspect(engine).get_columns(_TABLE)}
        with engine.connect() as connection:
            connection.execute(sa.select(SqlConversationMetadata).limit(1)).first()
        _migrate(uri, engine, "upgrade", _REVISION)
        column = SqlConversationMetadata.__table__.c[_COLUMN]
        assert column.nullable and column.server_default is None
        assert SqlConversationMetadata.launch_pricing_context_id.property.deferred
        assert _COLUMN in {column["name"] for column in sa.inspect(engine).get_columns(_TABLE)}
    finally:
        engine.dispose()


def test_launch_pricing_reference_preserves_legacy_rows_and_writes(tmp_path: Path) -> None:
    uri = f"sqlite:///{tmp_path / 'pricing.db'}"
    engine = sa.create_engine(uri)
    overrides = '{"model_override":"legacy-model","reasoning_effort":"high"}'
    context_id = bytes.fromhex("fedcba9876543210fedcba9876543210")
    orm_context_id = "1234567890abcdef1234567890abcdef"
    try:
        _migrate(uri, engine, "upgrade", _PREVIOUS)
        inspector = sa.inspect(engine)
        previous_columns = [
            {**column, "type": str(column["type"])} for column in inspector.get_columns(_TABLE)
        ]
        previous_indexes = inspector.get_indexes(_TABLE)
        previous_checks = inspector.get_check_constraints(_TABLE)
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "INSERT INTO conversations "
                    "(workspace_id, id, created_at, updated_at, root_conversation_id, "
                    "session_overrides) VALUES (0, :id, 1, 2, :id, :overrides)"
                ),
                {"id": _SESSION_ID, "overrides": overrides},
            )
            connection.execute(
                sa.text(
                    f"INSERT INTO {_TABLE} (workspace_id, id, kind, git_branch) "
                    "VALUES (0, :id, 1, 'main')"
                ),
                {"id": _SESSION_ID},
            )

        _migrate(uri, engine, "upgrade", _REVISION)
        columns = {column["name"]: column for column in sa.inspect(engine).get_columns(_TABLE)}
        assert set(columns) == {column["name"] for column in previous_columns} | {_COLUMN}
        assert columns[_COLUMN]["nullable"] is True
        assert columns[_COLUMN]["default"] is None
        assert isinstance(columns[_COLUMN]["type"], sa.LargeBinary)
        assert sa.inspect(engine).get_indexes(_TABLE) == previous_indexes
        with engine.begin() as connection:
            assert connection.scalar(sa.text(f"SELECT {_COLUMN} FROM {_TABLE}")) is None
            connection.execute(
                sa.text(f"INSERT INTO {_TABLE} (workspace_id, id, kind) VALUES (0, :id, 1)"),
                {"id": context_id},
            )
            connection.execute(
                sa.text(
                    f"UPDATE {_TABLE} SET {_COLUMN} = :context WHERE workspace_id = 0 AND id = :id"
                ),
                {"id": _SESSION_ID, "context": context_id},
            )
            connection.execute(
                sa.text(f"UPDATE {_TABLE} SET git_branch = 'legacy-write' WHERE id = :id"),
                {"id": _SESSION_ID},
            )
            stored = dict(connection.execute(sa.text(f"SELECT id, {_COLUMN} FROM {_TABLE}")).all())
            assert stored == {_SESSION_ID: context_id, context_id: None}
            row = connection.execute(
                sa.text("SELECT session_overrides, updated_at FROM conversations WHERE id = :id"),
                {"id": _SESSION_ID},
            ).one()
            assert tuple(row) == (overrides, 2)

        with Session(engine) as session:
            metadata = session.get(SqlConversationMetadata, (0, _SESSION_ID.hex()))
            assert metadata is not None
            assert metadata.launch_pricing_context_id == context_id.hex()
            metadata.launch_pricing_context_id = orm_context_id
            session.commit()
        with Session(engine) as session:
            metadata = session.get(SqlConversationMetadata, (0, _SESSION_ID.hex()))
            assert metadata is not None
            assert metadata.launch_pricing_context_id == orm_context_id

        _migrate(uri, engine, "downgrade", _PREVIOUS)
        inspector = sa.inspect(engine)
        assert [
            {**column, "type": str(column["type"])} for column in inspector.get_columns(_TABLE)
        ] == previous_columns
        assert inspector.get_indexes(_TABLE) == previous_indexes
        assert inspector.get_check_constraints(_TABLE) == previous_checks
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.text(f"SELECT git_branch FROM {_TABLE} WHERE id = :id"), {"id": _SESSION_ID}
                )
                == "legacy-write"
            )
    finally:
        engine.dispose()
