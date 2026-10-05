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


def _watermarks(engine: sa.Engine) -> dict[tuple[int, bytes], tuple[int | None, int | None]]:
    """Read the staged nullable columns using raw SQL."""
    with engine.connect() as connection:
        return {
            (row.workspace_id, bytes(row.id)): (
                row.last_message_at,
                row.last_message_observed_position,
            )
            for row in connection.execute(
                sa.text(
                    "SELECT workspace_id, id, last_message_at, "
                    "last_message_observed_position FROM conversations"
                )
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
        assert {"last_message_at", "last_message_observed_position"} <= set(columns)
        assert columns["last_message_at"]["nullable"] is True
        assert columns["last_message_observed_position"]["nullable"] is True
        assert _base_rows(engine) == before
        assert _watermarks(engine) == {
            (0, b"\x01" * 16): (None, None),
            (0, b"\x02" * 16): (None, None),
            (17, b"\x01" * 16): (None, None),
        }

        # Application reconciliation may populate a marker before a migration
        # process retries; the DDL-only re-entry must leave it untouched.
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    "UPDATE conversations SET last_message_at = :last_message_at, "
                    "last_message_observed_position = :observed "
                    "WHERE workspace_id = :workspace_id AND id = :id"
                ),
                {
                    "last_message_at": 777,
                    "observed": 42,
                    "workspace_id": 0,
                    "id": b"\x01" * 16,
                },
            )
        _direct_upgrade(engine, migration)
        assert _watermarks(engine)[(0, b"\x01" * 16)] == (777, 42)
        assert _base_rows(engine) == before

        _migrate(engine, db_uri, _PREVIOUS_REVISION, downgrade=True)
        assert not {"last_message_at", "last_message_observed_position"} & {
            column["name"] for column in sa.inspect(engine).get_columns("conversations")
        }
        assert _base_rows(engine) == before
    finally:
        _migrate(engine, db_uri, "head")
