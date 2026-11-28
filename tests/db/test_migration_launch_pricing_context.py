"""Launch pricing storage is additive and preserves existing session fields."""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.orm import Session

from omnigent.db.cockroachdb import _crdb_server_version, _prepare_crdb_schema_transaction
from omnigent.db.db_models import ConversationBase, OmnigentBase, SqlConversationMetadata, Uuid16
from omnigent.db.utils import (
    _build_alembic_config,
    _get_current_db_revision,
    _get_head_db_revision,
    _initialize_or_verify_schema,
    get_or_create_engine,
)

_PREVIOUS = "47d3e8fc3e25"
_REVISION = "c51f8d9a27b6"
_TABLE = "omnigent_conversation_metadata"
_COLUMN = "launch_pricing_context_id"
_SESSION_ID = bytes.fromhex("0123456789abcdef0123456789abcdef")


def _migrate(uri: str, engine: sa.Engine, step: str, revision: str) -> None:
    config = _build_alembic_config(uri)
    with engine.connect() as connection:
        if engine.dialect.name == "cockroachdb":
            _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
        config.attributes["connection"] = connection
        getattr(command, step)(config, revision)
        connection.commit()


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


@pytest.mark.parametrize("interruption", ["committed-ddl", "after-ddl"])
def test_startup_recovers_an_interrupted_pricing_reference_upgrade(
    db_uri: str, interruption: str, record_property: Callable[[str, object], None]
) -> None:
    """Resume committed DDL and a failure immediately after the column is added."""
    engine = get_or_create_engine(db_uri)
    head = _get_head_db_revision(db_uri)
    context_id = bytes.fromhex("fedcba9876543210fedcba9876543210")
    stopped_after_ddl: list[str] = []

    class MigrationInterrupted(RuntimeError):
        pass

    def interrupt_after_ddl(connection, cursor, statement, parameters, context, executemany):
        if statement.startswith(f"ALTER TABLE {_TABLE} ADD COLUMN {_COLUMN}"):
            stopped_after_ddl.append(statement)
            raise MigrationInterrupted("Stopped after the database accepted the column DDL")

    try:
        _migrate(db_uri, engine, "downgrade", _REVISION)
        assert _get_current_db_revision(engine) == _REVISION
        if interruption == "after-ddl":
            _migrate(db_uri, engine, "downgrade", _PREVIOUS)
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    f"INSERT INTO {_TABLE} (workspace_id, id, kind, git_branch) "
                    "VALUES (0, :id, 1, 'before-restart')"
                ),
                {"id": _SESSION_ID},
            )
            if interruption == "committed-ddl":
                connection.execute(
                    sa.text(f"UPDATE {_TABLE} SET {_COLUMN} = :context WHERE id = :id"),
                    {"context": context_id, "id": _SESSION_ID},
                )
        if interruption == "committed-ddl":
            _migrate(db_uri, engine, "stamp", _PREVIOUS)
        else:
            sa.event.listen(engine, "after_cursor_execute", interrupt_after_ddl)
            try:
                with pytest.raises(MigrationInterrupted, match="accepted the column DDL"):
                    _migrate(db_uri, engine, "upgrade", _REVISION)
            finally:
                sa.event.remove(engine, "after_cursor_execute", interrupt_after_ddl)
            assert len(stopped_after_ddl) == 1
        assert _get_current_db_revision(engine) == _PREVIOUS
        column_survived = _COLUMN in {
            column["name"] for column in sa.inspect(engine).get_columns(_TABLE)
        }
        if interruption == "committed-ddl" or engine.dialect.name == "mysql":
            assert column_survived

        _initialize_or_verify_schema(engine, db_uri)
        _initialize_or_verify_schema(engine, db_uri)

        assert _get_current_db_revision(engine) == head
        with engine.begin() as connection:
            row = connection.execute(
                sa.text(f"SELECT git_branch, {_COLUMN} FROM {_TABLE} WHERE id = :id"),
                {"id": _SESSION_ID},
            ).one()
            assert tuple(row) == (
                "before-restart",
                context_id if interruption == "committed-ddl" else None,
            )
            connection.execute(
                sa.text(f"UPDATE {_TABLE} SET git_branch = 'after-restart' WHERE id = :id"),
                {"id": _SESSION_ID},
            )
        record_property(
            "launch_pricing_recovery",
            json.dumps(
                {
                    "dialect": engine.dialect.name,
                    "interruption": interruption,
                    "column_survived_interruption": column_survived,
                    "stopped_after_ddl": len(stopped_after_ddl),
                    "startup_reached_revision": head,
                    "legacy_row_and_reference_preserved": True,
                    "legacy_write_accepted": True,
                    "scope": "Owned test database; not deployment or older-binary acceptance",
                }
            ),
        )
    finally:
        if _get_current_db_revision(engine) == _PREVIOUS:
            present = _COLUMN in {
                column["name"] for column in sa.inspect(engine).get_columns(_TABLE)
            }
            _migrate(db_uri, engine, "stamp" if present else "upgrade", _REVISION)
        _migrate(db_uri, engine, "upgrade", head)


@pytest.mark.parametrize("incompatible", ["wrong-type", "not-null"])
def test_startup_preserves_an_incompatible_existing_pricing_reference(
    db_uri: str, incompatible: str, record_property: Callable[[str, object], None]
) -> None:
    engine = get_or_create_engine(db_uri)
    head = _get_head_db_revision(db_uri)
    _migrate(db_uri, engine, "downgrade", _PREVIOUS)
    value = 17 if incompatible == "wrong-type" else _SESSION_ID
    statements: list[str] = []

    def record(connection, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    try:
        with engine.connect() as connection:
            if engine.dialect.name == "cockroachdb":
                _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
            operation = Operations(MigrationContext.configure(connection))
            operation.add_column(
                _TABLE,
                sa.Column(
                    _COLUMN,
                    sa.Integer() if incompatible == "wrong-type" else Uuid16(),
                    nullable=incompatible != "not-null",
                ),
            )
            connection.commit()
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    f"INSERT INTO {_TABLE} (workspace_id, id, kind, {_COLUMN}) "
                    "VALUES (0, :id, 1, :context)"
                ),
                {"id": _SESSION_ID, "context": value},
            )
        sa.event.listen(engine, "before_cursor_execute", record)
        try:
            with pytest.raises(RuntimeError) as error:
                _initialize_or_verify_schema(engine, db_uri)
        finally:
            sa.event.remove(engine, "before_cursor_execute", record)
        cause = error.value
        while cause.__cause__ is not None:
            cause = cause.__cause__
        assert "launch_pricing_context_id column has an incompatible schema" in str(cause)
        assert not any(
            statement.lstrip().upper().startswith(("ALTER TABLE", "UPDATE "))
            for statement in statements
        )
        assert _get_current_db_revision(engine) == _PREVIOUS
        with engine.connect() as connection:
            assert (
                connection.scalar(
                    sa.text(f"SELECT {_COLUMN} FROM {_TABLE} WHERE id = :id"),
                    {"id": _SESSION_ID},
                )
                == value
            )
        record_property(
            "launch_pricing_incompatible_schema",
            json.dumps(
                {
                    "dialect": engine.dialect.name,
                    "incompatible": incompatible,
                    "existing_value_preserved": True,
                    "revision_unchanged": True,
                    "alter_or_version_update_attempts": 0,
                }
            ),
        )
    finally:
        if _COLUMN in {column["name"] for column in sa.inspect(engine).get_columns(_TABLE)}:
            with engine.connect() as connection:
                if engine.dialect.name == "cockroachdb":
                    _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
                operation = Operations(MigrationContext.configure(connection))
                with operation.batch_alter_table(_TABLE) as batch:
                    batch.drop_column(_COLUMN)
                connection.commit()
        _migrate(db_uri, engine, "upgrade", head)
