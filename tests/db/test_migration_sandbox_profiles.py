"""Sandbox profile schema is additive and agrees with ORM metadata on each dialect."""

from __future__ import annotations

from importlib import import_module
from io import StringIO
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.schema import CreateIndex, CreateTable

from omnigent.db.db_models import OmnigentBase, SqlSandboxProfile, SqlSandboxProfileBinding

_MIGRATION = import_module("omnigent.db.migrations.versions.nn1a2b3c4d5e_add_sandbox_profiles")
_TABLES = (SqlSandboxProfile.__table__, SqlSandboxProfileBinding.__table__)


def test_sqlite_profile_migration_is_additive_and_workspace_scoped(tmp_path: Path) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'sandbox-profiles.db'}")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE existing_data (value TEXT NOT NULL)"))
            connection.execute(sa.text("INSERT INTO existing_data VALUES ('preserve me')"))
            with Operations.context(MigrationContext.configure(connection)):
                _MIGRATION.upgrade()
            for workspace in (0, 42):
                connection.execute(
                    SqlSandboxProfile.__table__.insert().values(
                        workspace_id=workspace,
                        id="build",
                        name="Build",
                        description="",
                        provider="kubernetes",
                        config_json=b"{}",
                        revision="a" * 64,
                        enabled=True,
                    )
                )
                connection.execute(
                    SqlSandboxProfileBinding.__table__.insert().values(
                        workspace_id=workspace,
                        session_id="b" * 32,
                        host_id=None,
                        snapshot_json=b"x" * 16383,
                    )
                )
            inspector = sa.inspect(connection)
            for table in _TABLES:
                assert table.metadata is OmnigentBase.metadata
                assert inspector.get_pk_constraint(table.name)["constrained_columns"] == [
                    column.name for column in table.primary_key
                ]
                assert not inspector.get_foreign_keys(table.name)
                assert connection.scalar(sa.select(sa.func.count()).select_from(table)) == 2
            binding_columns = {
                column["name"]: column
                for column in inspector.get_columns("sandbox_profile_bindings")
            }
            assert binding_columns["host_id"]["nullable"]
            assert not binding_columns["snapshot_json"]["nullable"]
            with Operations.context(MigrationContext.configure(connection)):
                _MIGRATION.downgrade()
            assert sa.inspect(connection).get_table_names() == ["existing_data"]
            assert connection.scalar(sa.text("SELECT value FROM existing_data")) == "preserve me"
    finally:
        engine.dispose()


@pytest.mark.parametrize("dialect", ["sqlite", "mysql", "postgresql"])
def test_profile_migration_ddl_matches_models(dialect: str) -> None:
    output = StringIO()
    context = MigrationContext.configure(
        dialect_name=dialect, opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        _MIGRATION.upgrade()
    ddl = " ".join(output.getvalue().split())
    assert ddl.count("CREATE TABLE") == 2
    assert ddl.count("CREATE INDEX") == 2
    assert "ALTER TABLE" not in ddl
    for table in _TABLES:
        assert " ".join(str(CreateTable(table).compile(dialect=context.dialect)).split()) in ddl
        for index in table.indexes:
            assert (
                " ".join(str(CreateIndex(index).compile(dialect=context.dialect)).split()) in ddl
            )
    if dialect == "mysql":
        assert "session_id BINARY(16) NOT NULL" in ddl
        assert "config_json BLOB(16383) NOT NULL" in ddl
        assert "snapshot_json BLOB(16383) NOT NULL" in ddl
