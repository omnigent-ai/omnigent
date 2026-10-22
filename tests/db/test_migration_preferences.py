"""Project order migration copies opaque data and survives failed preference copies."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from importlib import import_module
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import mysql, postgresql, sqlite

from omnigent.db.compression import encode


@pytest.fixture
def migration() -> ModuleType:
    return import_module(
        "omnigent.db.migrations.versions.jj1a2b3c4d5e_move_project_order_to_preferences"
    )


@pytest.fixture
def migration_engine(tmp_path: Path) -> Iterator[sa.Engine]:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'preferences.db'}")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE users (workspace_id BIGINT NOT NULL, id VARCHAR(128) NOT NULL, "
                "is_admin BOOLEAN NOT NULL DEFAULT false, password_hash VARCHAR(256), "
                "project_order BLOB, PRIMARY KEY (workspace_id, id))"
            )
        )
    yield engine
    engine.dispose()


def _run(engine: sa.Engine, migration: ModuleType, direction: str = "upgrade") -> None:
    with engine.begin() as connection:
        with Operations.context(MigrationContext.configure(connection)):
            getattr(migration, direction)()


def _seed_users(engine: sa.Engine, values: list[tuple[int, str, bytes | str | None]]) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO users (workspace_id, id, project_order) "
                "VALUES (:workspace_id, :id, :value)"
            ),
            [
                {"workspace_id": workspace_id, "id": id, "value": value}
                for workspace_id, id, value in values
            ],
        )


def _create_preferences(engine: sa.Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE preferences (workspace_id BIGINT NOT NULL, "
                "user_id VARCHAR(128) NOT NULL, key VARCHAR(128) NOT NULL, "
                "value BLOB NOT NULL, PRIMARY KEY (workspace_id, user_id, key))"
            )
        )


def _preferences(engine: sa.Engine) -> dict[tuple[int, str, str], bytes | str]:
    with engine.connect() as connection:
        return {
            (row.workspace_id, row.user_id, row.key): row.value
            for row in connection.execute(sa.text("SELECT * FROM preferences"))
        }


def test_migration_preserves_binary_legacy_and_invalid_values(
    migration_engine: sa.Engine, migration: ModuleType
) -> None:
    values = [
        (0, "alice", encode('{"sort_mode":"manual","ordered_project_ids":["' + "a" * 32 + '"]}')),
        (17, "alice", b'["' + b"b" * 32 + b'"]'),
        (0, "local", b"\x00\x01broken zstd frame"),
        (0, "large", bytes(range(256)) * 300),
        (0, "legacy-text", "[]"),
        (0, "unset", None),
    ]
    _seed_users(migration_engine, values)
    _run(migration_engine, migration)
    assert _preferences(migration_engine) == {
        (workspace_id, user_id, "project_order"): value
        for workspace_id, user_id, value in values
        if value is not None
    }
    inspector = sa.inspect(migration_engine)
    assert inspector.get_pk_constraint("preferences")["constrained_columns"] == [
        "workspace_id",
        "user_id",
        "key",
    ]
    assert "project_order" not in {column["name"] for column in inspector.get_columns("users")}
    _run(migration_engine, migration, "downgrade")
    with migration_engine.connect() as connection:
        restored = connection.execute(
            sa.text("SELECT workspace_id, id, project_order FROM users")
        ).all()
    assert set(restored) == set(values)
    assert "preferences" not in sa.inspect(migration_engine).get_table_names()


def test_upgrade_resumes_and_preserves_existing_destination_values(
    migration_engine: sa.Engine, migration: ModuleType
) -> None:
    _seed_users(migration_engine, [(0, "alice", b"old"), (1, "alice", b"workspace-one")])
    _create_preferences(migration_engine)
    with migration_engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO preferences VALUES (0, 'alice', 'project_order', :value)"),
            {"value": b"new"},
        )
        connection.execute(
            sa.text("INSERT INTO preferences VALUES (0, 'alice', 'theme', :value)"),
            {"value": b"dark"},
        )
    _run(migration_engine, migration)
    expected = {
        (0, "alice", "project_order"): b"new",
        (1, "alice", "project_order"): b"workspace-one",
        (0, "alice", "theme"): b"dark",
    }
    assert _preferences(migration_engine) == expected
    _run(migration_engine, migration)
    assert _preferences(migration_engine) == expected


def test_upgrade_without_source_column_creates_preferences(
    migration_engine: sa.Engine, migration: ModuleType
) -> None:
    with migration_engine.begin() as connection:
        operations = Operations(MigrationContext.configure(connection))
        with operations.batch_alter_table("users") as batch:
            batch.drop_column("project_order")
    _run(migration_engine, migration)
    _run(migration_engine, migration)
    assert _preferences(migration_engine) == {}


@pytest.mark.parametrize("failures", [1, 2, 3])
def test_copy_retries_in_savepoints_and_drops_source_on_exhaustion(
    migration_engine: sa.Engine,
    migration: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failures: int,
) -> None:
    _seed_users(migration_engine, [(0, "alice", b"order")])
    attempts = 0
    rollbacks = 0
    delays: list[float] = []
    monkeypatch.setattr(migration.time, "sleep", delays.append)

    def fail_copy(conn, cursor, statement, parameters, context, executemany):
        nonlocal attempts, rollbacks
        if statement.startswith("ROLLBACK TO SAVEPOINT"):
            rollbacks += 1
        if statement.startswith("INSERT INTO preferences"):
            attempts += 1
            if attempts <= failures:
                return "INSERT INTO missing_migration_table SELECT 1", ()
        return statement, parameters

    sa.event.listen(migration_engine, "before_cursor_execute", fail_copy, retval=True)
    try:
        with caplog.at_level(logging.WARNING, logger=migration.__name__):
            _run(migration_engine, migration)
    finally:
        sa.event.remove(migration_engine, "before_cursor_execute", fail_copy)
    assert attempts == min(failures + 1, 3)
    assert rollbacks == failures
    assert delays == [0.1, 0.2][: min(failures, 2)]
    assert "project_order" not in {
        column["name"] for column in sa.inspect(migration_engine).get_columns("users")
    }
    if failures == 3:
        assert _preferences(migration_engine) == {}
        assert "after 3 attempts" in caplog.text
        assert "continuing with source removal" in caplog.text
    else:
        assert _preferences(migration_engine) == {(0, "alice", "project_order"): b"order"}
        assert not caplog.records


@pytest.mark.parametrize("phase", ["create", "drop"])
def test_schema_failure_is_not_swallowed(
    migration_engine: sa.Engine,
    migration: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
) -> None:
    _seed_users(migration_engine, [(0, "alice", b"order")])
    delays: list[float] = []
    monkeypatch.setattr(migration.time, "sleep", delays.append)

    def fail_create(conn, cursor, statement, parameters, context, executemany):
        if (phase == "create" and "CREATE TABLE preferences" in statement) or (
            phase == "drop" and statement.strip().startswith("DROP TABLE users")
        ):
            return "CREATE TABLE invalid definition", ()
        return statement, parameters

    sa.event.listen(migration_engine, "before_cursor_execute", fail_create, retval=True)
    try:
        with pytest.raises(sa.exc.SQLAlchemyError):
            _run(migration_engine, migration)
    finally:
        sa.event.remove(migration_engine, "before_cursor_execute", fail_create)
    assert delays == []
    assert "project_order" in {
        column["name"] for column in sa.inspect(migration_engine).get_columns("users")
    }
    assert "_alembic_tmp_users" not in sa.inspect(migration_engine).get_table_names()
    _run(migration_engine, migration)
    assert _preferences(migration_engine) == {(0, "alice", "project_order"): b"order"}
    assert "project_order" not in {
        column["name"] for column in sa.inspect(migration_engine).get_columns("users")
    }


def test_downgrade_restores_preference_only_users_and_preserves_existing_values(
    migration_engine: sa.Engine, migration: ModuleType
) -> None:
    _seed_users(migration_engine, [(0, "alice", b"old-source")])
    _create_preferences(migration_engine)
    with migration_engine.begin() as connection:
        connection.execute(
            sa.text(
                "INSERT INTO preferences VALUES (:workspace_id, :user_id, 'project_order', :value)"
            ),
            [
                {"workspace_id": 0, "user_id": "alice", "value": b"destination"},
                {"workspace_id": 0, "user_id": "local", "value": b"local-order"},
                {"workspace_id": 7, "user_id": "alice", "value": b"other-workspace"},
            ],
        )
        connection.execute(
            sa.text(
                "UPDATE users SET is_admin=true, password_hash='existing-hash' WHERE id='alice'"
            )
        )
    _run(migration_engine, migration, "downgrade")
    _run(migration_engine, migration, "downgrade")
    with migration_engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT workspace_id, id, is_admin, password_hash, project_order FROM users")
        ).all()
    assert set(rows) == {
        (0, "alice", True, "existing-hash", b"old-source"),
        (0, "local", False, None, b"local-order"),
        (7, "alice", False, None, b"other-workspace"),
    }


@pytest.mark.parametrize("failures", [2, 3])
def test_downgrade_retries_roll_back_partial_copies(
    migration_engine: sa.Engine,
    migration: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failures: int,
) -> None:
    _seed_users(migration_engine, [(0, "alice", None)])
    _create_preferences(migration_engine)
    with migration_engine.begin() as connection:
        connection.execute(
            sa.text("INSERT INTO preferences VALUES (0, :user_id, 'project_order', :value)"),
            [
                {"user_id": "alice", "value": b"alice-order"},
                {"user_id": "local", "value": b"local-order"},
            ],
        )
    attempts = 0
    delays: list[float] = []
    monkeypatch.setattr(migration.time, "sleep", delays.append)

    def fail_second_statement(conn, cursor, statement, parameters, context, executemany):
        nonlocal attempts
        if statement.startswith("UPDATE users SET project_order="):
            attempts += 1
            if attempts <= failures:
                return "UPDATE missing_migration_table SET value=1", ()
        return statement, parameters

    sa.event.listen(migration_engine, "before_cursor_execute", fail_second_statement, retval=True)
    try:
        with caplog.at_level(logging.WARNING, logger=migration.__name__):
            _run(migration_engine, migration, "downgrade")
    finally:
        sa.event.remove(migration_engine, "before_cursor_execute", fail_second_statement)
    assert attempts == 3
    assert delays == [0.1, 0.2]
    assert "preferences" not in sa.inspect(migration_engine).get_table_names()
    with migration_engine.connect() as connection:
        rows = dict(connection.execute(sa.text("SELECT id, project_order FROM users")).all())
    if failures == 3:
        assert rows == {"alice": None}
        assert "restore project order preferences after 3 attempts" in caplog.text
    else:
        assert rows == {"alice": b"alice-order", "local": b"local-order"}
        assert not caplog.records


@pytest.mark.parametrize("dialect", [sqlite.dialect(), postgresql.dialect(), mysql.dialect()])
def test_migration_statements_compile_without_value_transformation(
    migration: ModuleType, dialect: sa.engine.Dialect
) -> None:
    copy = str(migration._copy_to_preferences().compile(dialect=dialect))
    assert "INSERT INTO preferences" in copy
    assert "users.project_order" in copy
    assert "LEFT OUTER JOIN preferences" in copy
    assert "preferences.user_id IS NULL" in copy
    for statement in migration._restore_project_orders():
        assert "preferences.value" in str(statement.compile(dialect=dialect))
    if dialect.name == "mysql":
        assert migration._BINARY.compile(dialect=dialect) == "MEDIUMBLOB"
        assert "`key`" in copy


@pytest.mark.parametrize("failure_point", ["statement", "commit"])
@pytest.mark.parametrize("failures", [2, 3])
def test_cockroachdb_copy_retries_entire_transaction(
    migration: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_point: str,
    failures: int,
) -> None:
    connection = MagicMock(spec=sa.Connection)
    connection.dialect = SimpleNamespace(name="cockroachdb")
    active = True
    failed = False
    attempts = 0
    delays: list[float] = []
    monkeypatch.setattr(migration.time, "sleep", delays.append)
    connection.in_transaction.side_effect = lambda: active

    def serialization_failure() -> None:
        nonlocal failed
        failed = True
        raise sa.exc.OperationalError("copy", {}, RuntimeError("restart transaction"))

    def execute(statement) -> None:
        nonlocal active, attempts
        assert not failed, "aborted transactions must be rolled back before reuse"
        if str(statement).startswith("SET TRANSACTION"):
            assert not active
            active = True
            return
        assert active
        attempts += 1
        if failure_point == "statement" and attempts <= failures:
            serialization_failure()

    def commit() -> None:
        nonlocal active
        assert not failed
        if active and failure_point == "commit" and attempts <= failures:
            serialization_failure()
        active = False

    def rollback() -> None:
        nonlocal active, failed
        active = False
        failed = False

    connection.execute.side_effect = execute
    connection.commit.side_effect = commit
    connection.rollback.side_effect = rollback
    with caplog.at_level(logging.WARNING, logger=migration.__name__):
        migration._copy_with_retries(connection, [sa.text("copy")], operation="migrate")
    assert attempts == 3
    assert connection.rollback.call_count == failures
    connection.begin_nested.assert_not_called()
    assert delays == [0.1, 0.2]
    assert not active
    if failures == 3:
        assert "after 3 attempts" in caplog.text
    else:
        assert not caplog.records
    migration._publish_crdb_changes(connection)
    assert active
