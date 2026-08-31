"""DDL coverage for the conversation-item source identity migration."""

from __future__ import annotations

import io
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.migrations.versions import (
    f3c7a1d9e2b4_add_conversation_item_source_id as migration,
)


def _compile_upgrade(monkeypatch: pytest.MonkeyPatch, dialect_name: str) -> str:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name=dialect_name,
        opts={"as_sql": True, "output_buffer": output},
    )
    monkeypatch.setattr(migration, "op", Operations(context))
    with context.begin_transaction():
        migration.upgrade()
    return output.getvalue()


def test_postgresql_source_index_is_concurrent_partial_and_autocommitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL must not build the large lookup index in a transaction."""
    ddl = _compile_upgrade(monkeypatch, "postgresql")

    index = ddl.index("CREATE INDEX CONCURRENTLY")
    assert ddl.rfind("COMMIT", 0, index) >= 0
    assert "WHERE source_id IS NOT NULL" in ddl[index:]


@pytest.mark.parametrize(
    ("dialect_name", "partial"),
    [("sqlite", True), ("mysql", False)],
)
def test_source_index_ddl_remains_portable(
    monkeypatch: pytest.MonkeyPatch,
    dialect_name: str,
    partial: bool,
) -> None:
    """SQLite gets its supported partial form; MySQL gets a plain index."""
    ddl = _compile_upgrade(monkeypatch, dialect_name)

    assert "CREATE INDEX ix_conversation_items_source_id" in ddl
    assert ("WHERE source_id IS NOT NULL" in ddl) is partial
    assert "CONCURRENTLY" not in ddl


class _FakeMappings:
    """Minimal mapping result used by the restart simulation."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row

    def mappings(self) -> _FakeMappings:
        return self

    def first(self) -> dict[str, Any] | None:
        return self._row


class _RestartablePostgresOperations:
    """Stateful Alembic-operation fake that leaves an invalid first index."""

    def __init__(self) -> None:
        self.column_exists = False
        self.index: dict[str, Any] | None = None
        self.create_attempts = 0
        self.drop_attempts = 0
        self.context = SimpleNamespace(
            as_sql=False,
            dialect=SimpleNamespace(name="postgresql"),
            autocommit_block=nullcontext,
        )
        self.bind = SimpleNamespace(execute=self._catalog_query)

    def get_context(self) -> Any:
        return self.context

    def get_bind(self) -> Any:
        return self.bind

    def add_column(self, table: str, column: Any) -> None:
        assert table == "conversation_items"
        assert column.name == "source_id"
        assert not self.column_exists
        self.column_exists = True

    def execute(self, statement: str) -> None:
        if statement.startswith("DROP INDEX CONCURRENTLY"):
            self.drop_attempts += 1
            self.index = None
            return
        assert statement.startswith("CREATE INDEX CONCURRENTLY")
        self.create_attempts += 1
        definition = (
            "CREATE INDEX ix_conversation_items_source_id ON conversation_items "
            "USING btree (workspace_id, conversation_id, source_id) "
            "WHERE (source_id IS NOT NULL)"
        )
        if self.create_attempts == 1:
            self.index = {"indisvalid": False, "definition": definition}
            raise RuntimeError("concurrent build cancelled")
        self.index = {"indisvalid": True, "definition": definition}

    def _catalog_query(self, statement: Any, params: dict[str, str]) -> _FakeMappings:
        assert "pg_get_indexdef" in str(statement)
        assert params["name"] == "ix_conversation_items_source_id"
        return _FakeMappings(self.index)


def test_postgresql_migration_recovers_after_cancelled_index_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A committed column and invalid index are repaired on migration retry."""
    operations = _RestartablePostgresOperations()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(
        migration,
        "_source_id_column_exists",
        lambda: operations.column_exists,
    )
    stamped = False

    with pytest.raises(RuntimeError, match="cancelled"):
        migration.upgrade()
        stamped = True

    assert operations.column_exists is True
    assert operations.index is not None
    assert operations.index["indisvalid"] is False
    assert stamped is False

    migration.upgrade()
    stamped = True

    assert operations.create_attempts == 2
    assert operations.drop_attempts == 1
    assert operations.index is not None
    assert operations.index["indisvalid"] is True
    assert stamped is True


def _create_minimal_conversation_items(engine: sa.Engine) -> None:
    """Create the columns required to compile and inspect the source index."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE conversation_items ("
            "id VARCHAR(64) PRIMARY KEY, "
            "workspace_id VARCHAR(64) NOT NULL, "
            "conversation_id VARCHAR(64) NOT NULL"
            ")"
        )


def test_sqlite_upgrade_and_downgrade_are_restartable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Committed SQLite DDL can be retried without an Alembic stamp."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'restartable.db'}")
    _create_minimal_conversation_items(engine)
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        monkeypatch.setattr(migration, "op", Operations(context))

        migration.upgrade()
        connection.commit()
        migration.upgrade()
        connection.commit()

        inspector = sa.inspect(connection)
        assert "source_id" in {
            column["name"] for column in inspector.get_columns("conversation_items")
        }
        indexes = {index["name"]: index for index in inspector.get_indexes("conversation_items")}
        source_index = indexes["ix_conversation_items_source_id"]
        assert tuple(source_index["column_names"]) == (
            "workspace_id",
            "conversation_id",
            "source_id",
        )
        assert "source_id IS NOT NULL" in str(source_index["dialect_options"]["sqlite_where"])

        migration.downgrade()
        connection.commit()
        migration.downgrade()
        connection.commit()
        assert "source_id" not in {
            column["name"] for column in sa.inspect(connection).get_columns("conversation_items")
        }
    engine.dispose()


def test_sqlite_upgrade_replaces_wrong_shape_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A matching index name cannot hide incompatible columns or predicate."""
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'wrong-index.db'}")
    _create_minimal_conversation_items(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "ALTER TABLE conversation_items ADD COLUMN source_id VARCHAR(512)"
        )
        connection.exec_driver_sql(
            "CREATE INDEX ix_conversation_items_source_id ON conversation_items (conversation_id)"
        )
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        monkeypatch.setattr(migration, "op", Operations(context))
        migration.upgrade()
        connection.commit()
        indexes = {
            index["name"]: index
            for index in sa.inspect(connection).get_indexes("conversation_items")
        }
        source_index = indexes["ix_conversation_items_source_id"]
        assert tuple(source_index["column_names"]) == (
            "workspace_id",
            "conversation_id",
            "source_id",
        )
        assert "source_id IS NOT NULL" in str(source_index["dialect_options"]["sqlite_where"])
    engine.dispose()


class _PortableOperations:
    """Minimal online operation state for MySQL restart behavior."""

    def __init__(self) -> None:
        self.column_exists = False
        self.index_exists = False
        self.index_current = False
        self.add_attempts = 0
        self.create_attempts = 0
        self.drop_attempts = 0
        self.drop_column_attempts = 0
        self.context = SimpleNamespace(
            as_sql=False,
            dialect=SimpleNamespace(name="mysql"),
        )

    def get_context(self) -> Any:
        return self.context

    def add_column(self, table: str, column: Any) -> None:
        assert table == "conversation_items"
        assert column.name == "source_id"
        self.add_attempts += 1
        self.column_exists = True

    def create_index(
        self,
        name: str,
        table: str,
        columns: list[str],
        *,
        unique: bool,
    ) -> None:
        assert name == "ix_conversation_items_source_id"
        assert table == "conversation_items"
        assert tuple(columns) == ("workspace_id", "conversation_id", "source_id")
        assert unique is False
        self.create_attempts += 1
        self.index_exists = True
        self.index_current = True

    def drop_index(self, name: str, *, table_name: str) -> None:
        assert name == "ix_conversation_items_source_id"
        assert table_name == "conversation_items"
        self.drop_attempts += 1
        self.index_exists = False
        self.index_current = False

    def batch_alter_table(self, table: str) -> _PortableOperations:
        assert table == "conversation_items"
        return self

    def __enter__(self) -> _PortableOperations:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def drop_column(self, name: str) -> None:
        assert name == "source_id"
        self.drop_column_attempts += 1
        self.column_exists = False


def test_mysql_upgrade_retry_and_wrong_index_repair(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MySQL retries committed DDL and replaces a same-name wrong index."""
    operations = _PortableOperations()
    monkeypatch.setattr(migration, "op", operations)
    monkeypatch.setattr(
        migration,
        "_source_id_column_exists",
        lambda: operations.column_exists,
    )
    monkeypatch.setattr(
        migration,
        "_portable_index_status",
        lambda: (operations.index_exists, operations.index_current),
    )

    migration.upgrade()
    migration.upgrade()
    assert operations.add_attempts == 1
    assert operations.create_attempts == 1
    assert operations.drop_attempts == 0

    operations.index_current = False
    migration.upgrade()
    assert operations.add_attempts == 1
    assert operations.create_attempts == 2
    assert operations.drop_attempts == 1

    migration.downgrade()
    migration.downgrade()
    assert operations.drop_attempts == 2
    assert operations.drop_column_attempts == 1
