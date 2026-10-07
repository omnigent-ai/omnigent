"""Migration coverage for the staged visible-message watermark columns."""

from __future__ import annotations

from importlib import import_module
from typing import Any

import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.cockroachdb import _crdb_server_version, _prepare_crdb_schema_transaction
from omnigent.db.utils import _build_alembic_config, get_or_create_engine

_PREVIOUS_REVISION = "mm1a2b3c4d5e"
_MIGRATION = "omnigent.db.migrations.versions.mn1a2b3c4d5e_add_last_message_at_to_conversations"


def _migrate(engine: sa.Engine, db_uri: str, revision: str, *, downgrade: bool = False) -> None:
    """Run Alembic against the fixture's real backend connection."""
    config = _build_alembic_config(db_uri)
    with engine.connect() as connection:
        if engine.dialect.name == "cockroachdb":
            _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
        config.attributes["connection"] = connection
        (command.downgrade if downgrade else command.upgrade)(config, revision)
        connection.commit()


def _direct_upgrade(engine: sa.Engine, migration: Any) -> None:
    """Re-enter the revision directly without changing Alembic's version row."""
    with engine.connect() as connection:
        if engine.dialect.name == "cockroachdb":
            _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
        context = MigrationContext.configure(connection, opts={"transactional_ddl": True})
        with Operations.context(context), context.begin_transaction():
            migration.upgrade()
        connection.commit()


def _direct_downgrade(engine: sa.Engine, migration: Any) -> None:
    """Re-enter the downgrade directly for partial-DDL retry coverage."""
    with engine.connect() as connection:
        if engine.dialect.name == "cockroachdb":
            _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
        context = MigrationContext.configure(connection, opts={"transactional_ddl": True})
        with Operations.context(context), context.begin_transaction():
            migration.downgrade()
        connection.commit()


def _alter_watermark_column(engine: sa.Engine, name: str, *, add: bool) -> None:
    """Apply one schema operation to simulate a partially completed DDL run."""
    with engine.connect() as connection:
        if engine.dialect.name == "cockroachdb":
            _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
        context = MigrationContext.configure(connection, opts={"transactional_ddl": True})
        operations = Operations(context)
        with Operations.context(context), context.begin_transaction():
            if add:
                operations.add_column(
                    "conversations", sa.Column(name, sa.Integer(), nullable=True)
                )
            else:
                with operations.batch_alter_table("conversations") as batch:
                    batch.drop_column(name)
        connection.commit()


def _insert_conversations(engine: sa.Engine, rows: list[tuple[int, bytes, int]]) -> None:
    """Insert minimal rows through reflection, without current ORM models."""
    table = sa.Table("conversations", sa.MetaData(), autoload_with=engine)
    with engine.begin() as connection:
        connection.execute(
            table.insert(),
            [
                {
                    "workspace_id": workspace_id,
                    "id": conversation_id,
                    "root_conversation_id": conversation_id,
                    "created_at": updated_at - 1,
                    "updated_at": updated_at,
                }
                for workspace_id, conversation_id, updated_at in rows
            ],
        )


def _base_rows(engine: sa.Engine) -> dict[tuple[int, bytes], tuple[int, int, bytes]]:
    """Read pre-existing columns without relying on the current ORM model."""
    with engine.connect() as connection:
        return {
            (row.workspace_id, bytes(row.id)): (
                row.created_at,
                row.updated_at,
                bytes(row.root_conversation_id),
            )
            for row in connection.execute(
                sa.text(
                    "SELECT workspace_id, id, created_at, updated_at, root_conversation_id "
                    "FROM conversations"
                )
            )
        }


def _watermarks(engine: sa.Engine) -> dict[tuple[int, bytes], int | None]:
    """Read the staged nullable column using raw SQL."""
    with engine.connect() as connection:
        return {
            (row.workspace_id, bytes(row.id)): row.last_message_at
            for row in connection.execute(
                sa.text("SELECT workspace_id, id, last_message_at FROM conversations")
            )
        }


def test_last_message_at_columns_preserve_rows_and_reenter(db_uri: str) -> None:
    """DDL adds nullable fields, preserves markers on re-entry, and downgrades cleanly."""
    engine = get_or_create_engine(db_uri)
    migration = import_module(_MIGRATION)
    rows = [
        (0, b"\x01" * 16, 100),
        (0, b"\x02" * 16, 200),
        (17, b"\x01" * 16, 300),
    ]
    try:
        _migrate(engine, db_uri, _PREVIOUS_REVISION, downgrade=True)
        _insert_conversations(engine, rows)
        before = _base_rows(engine)
        _migrate(engine, db_uri, "head")

        columns = {
            column["name"]: column for column in sa.inspect(engine).get_columns("conversations")
        }
        assert "last_message_at" in columns
        assert columns["last_message_at"]["nullable"] is True
        assert _base_rows(engine) == before
        assert _watermarks(engine) == {
            (0, b"\x01" * 16): None,
            (0, b"\x02" * 16): None,
            (17, b"\x01" * 16): None,
        }

        # Application writers may populate a marker before a migration process
        # retries; the DDL-only re-entry must leave it untouched.
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "UPDATE conversations SET last_message_at = :last_message_at "
                    "WHERE workspace_id = :workspace_id AND id = :id"
                ),
                {
                    "last_message_at": 777,
                    "workspace_id": 0,
                    "id": b"\x01" * 16,
                },
            )
        _direct_upgrade(engine, migration)
        assert _watermarks(engine)[(0, b"\x01" * 16)] == 777
        assert _base_rows(engine) == before

        _migrate(engine, db_uri, _PREVIOUS_REVISION, downgrade=True)
        assert "last_message_at" not in {
            column["name"] for column in sa.inspect(engine).get_columns("conversations")
        }
        assert _base_rows(engine) == before
    finally:
        _migrate(engine, db_uri, "head")


def test_last_message_at_partial_ddl_retry_preserves_markers_and_data(db_uri: str) -> None:
    """Partial upgrades/downgrades preserve markers and old-style inserts."""
    engine = get_or_create_engine(db_uri)
    migration = import_module(_MIGRATION)
    rows = [(0, b"\x11" * 16, 500), (17, b"\x11" * 16, 600)]
    try:
        _migrate(engine, db_uri, _PREVIOUS_REVISION, downgrade=True)
        _insert_conversations(engine, rows)
        before = _base_rows(engine)

        # Simulate a process that added only the first column before it died.
        _alter_watermark_column(engine, "last_message_at", add=True)
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "UPDATE conversations SET last_message_at = :value "
                    "WHERE workspace_id = :workspace_id AND id = :id"
                ),
                {"value": 888, "workspace_id": 0, "id": b"\x11" * 16},
            )

        _migrate(engine, db_uri, "head")
        assert _watermarks(engine)[(0, b"\x11" * 16)] == 888
        assert _base_rows(engine) == before

        # A writer from before the schema release omits the new column.
        old_style_row = (23, b"\x12" * 16, 700)
        _insert_conversations(engine, [old_style_row])
        assert _watermarks(engine)[(23, b"\x12" * 16)] is None

        # Simulate a partial downgrade, then retry it and retry it again.
        _alter_watermark_column(engine, "last_message_at", add=False)
        _direct_downgrade(engine, migration)
        assert "last_message_at" not in {
            column["name"] for column in sa.inspect(engine).get_columns("conversations")
        }
        assert _base_rows(engine) == {**before, (23, b"\x12" * 16): (699, 700, b"\x12" * 16)}
        _direct_downgrade(engine, migration)
        assert "last_message_at" not in {
            column["name"] for column in sa.inspect(engine).get_columns("conversations")
        }
    finally:
        _direct_upgrade(engine, migration)
        _migrate(engine, db_uri, "head")
