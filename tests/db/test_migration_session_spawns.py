"""Migration, rollback and index contracts for pending child receipts."""

from __future__ import annotations

import importlib
import io
from collections.abc import Iterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory

from omnigent.db.db_models import (
    ConversationBase,
    SqlConversation,
    SqlFile,
    SqlSessionSpawn,
    SqlSessionSpawnEpoch,
    SqlSessionSpawnMaterialization,
    SqlSessionSpawnPayload,
)
from omnigent.db.utils import _build_alembic_config, _ensure_conversation_tables

_PREVIOUS = "27d16059fab9"
_BRANCH_POINT = "b4e2d7a91c35"
_REVISION = "e7a94c2d6b10"
_TABLES = {
    model.__table__.name: model.__table__
    for model in (
        SqlSessionSpawnEpoch,
        SqlSessionSpawn,
        SqlSessionSpawnPayload,
        SqlSessionSpawnMaterialization,
    )
}
_ROWS = {
    "session_spawn_epochs": {"workspace_id": 0, "parent_id": "1" * 32, "epoch": 7},
    "session_spawns": {
        "workspace_id": 0,
        "creator_runner_id": "runner-a",
        "epoch": 7,
        "nonce": "2" * 32,
        "child_id": "3" * 32,
        "parent_id": "1" * 32,
        "owner_id": "alice",
        "account_generation": None,
        "title": "pending-child",
        "request_digest": b"r" * 32,
        "name_digest": None,
        "state": 1,
        "phase": 1,
        "create_payload": None,
        "input_payload": None,
        "input_digest": None,
        "file_mapping": None,
        "cleanup_blob_keys": None,
        "created_at": 1,
        "updated_at": 1,
    },
    "session_spawn_payloads": {
        "workspace_id": 0,
        "id": "4" * 32,
        "child_id": "3" * 32,
        "payload_digest": b"p" * 32,
        "encoded_digest": b"e" * 32,
        "cleanup_after": None,
        "cleanup_attempts": 0,
    },
    "session_spawn_materializations": {
        "workspace_id": 0,
        "child_id": "3" * 32,
        "creator_runner_id": "runner-a",
        "epoch": 7,
        "nonce": "2" * 32,
        "create_digest": b"c" * 32,
        "created_at": 1,
    },
}


@pytest.fixture()
def database(tmp_path: Path) -> Iterator[tuple[str, sa.Engine]]:
    uri = f"sqlite:///{tmp_path / 'spawns.db'}"
    engine = sa.create_engine(uri)
    try:
        yield uri, engine
    finally:
        engine.dispose()


def _migrate(database: tuple[str, sa.Engine], revision: str, *, down: bool = False) -> None:
    uri, engine = database
    config = _build_alembic_config(uri)
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        (command.downgrade if down else command.upgrade)(config, revision)


def test_upgrade_matches_models_and_empty_rollback_preserves_existing_files(database) -> None:
    _migrate(database, _PREVIOUS)
    _, engine = database
    existing = {
        "workspace_id": 0,
        "id": "a" * 32,
        "created_at": 1,
        "filename": "kept.txt",
        "bytes": 3,
    }
    with engine.begin() as connection:
        connection.execute(SqlFile.__table__.insert(), existing)

    _migrate(database, _REVISION)
    inspector = sa.inspect(engine)
    for name, table in _TABLES.items():
        columns = inspector.get_columns(name)
        assert [column["name"] for column in columns] == list(table.columns.keys())
        assert {column["name"]: column["nullable"] for column in columns} == {
            column.name: column.nullable for column in table.columns
        }
        assert all(column["default"] is None for column in columns)
        assert inspector.get_pk_constraint(name)["constrained_columns"] == [
            column.name for column in table.primary_key
        ]
        assert inspector.get_foreign_keys(name) == []
        assert {index["name"]: index["column_names"] for index in inspector.get_indexes(name)} == {
            index.name: [column.name for column in index.columns] for index in table.indexes
        }
        assert {
            constraint["name"]: constraint["column_names"]
            for constraint in inspector.get_unique_constraints(name)
        } == {
            constraint.name: [column.name for column in constraint.columns]
            for constraint in table.constraints
            if isinstance(constraint, sa.UniqueConstraint)
        }

    indexes = {index["name"]: index["column_names"] for index in inspector.get_indexes("files")}
    assert indexes["ix_files_blob_reference"] == ["workspace_id", "blob_key", "id"]
    with engine.connect() as connection:
        assert connection.execute(sa.select(SqlFile.id, SqlFile.filename)).all() == [
            (existing["id"], existing["filename"])
        ]

    _migrate(database, _BRANCH_POINT, down=True)
    assert set(_TABLES).isdisjoint(sa.inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.execute(sa.select(SqlFile.id, SqlFile.filename)).all() == [
            (existing["id"], existing["filename"])
        ]
    _migrate(database, _REVISION)
    assert set(_TABLES) <= set(sa.inspect(engine).get_table_names())


def test_split_conversation_initialization_preserves_data_without_metadata_migrations(
    tmp_path: Path,
) -> None:
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'conversations.db'}")
    marker = SqlSessionSpawnMaterialization.__table__
    existing = {
        "workspace_id": 0,
        "id": "a" * 32,
        "root_conversation_id": "a" * 32,
        "created_at": 1,
        "updated_at": 1,
        "title": "kept conversation",
    }
    try:
        ConversationBase.metadata.create_all(
            engine,
            tables=[
                table for table in ConversationBase.metadata.sorted_tables if table is not marker
            ],
        )
        with engine.begin() as connection:
            connection.execute(SqlConversation.__table__.insert(), existing)

        _ensure_conversation_tables(engine)
        with engine.begin() as connection:
            connection.execute(marker.insert(), _ROWS[marker.name])
        _ensure_conversation_tables(engine)

        tables = set(sa.inspect(engine).get_table_names())
        assert marker.name in tables
        assert (set(_TABLES) - {marker.name}).isdisjoint(tables)
        assert "files" not in tables and "alembic_version" not in tables
        with engine.connect() as connection:
            assert connection.execute(
                sa.select(SqlConversation.id, SqlConversation.title)
            ).all() == [(existing["id"], existing["title"])]
            assert (
                connection.execute(sa.select(SqlSessionSpawnMaterialization.nonce)).scalar_one()
                == (_ROWS[marker.name]["nonce"])
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("previous", ["as1b2c3d4e5f", "c6e9a2b7d481"])
def test_existing_spawn_revision_upgrades_without_recreating_receipts(database, previous) -> None:
    _migrate(database, previous)
    _, engine = database
    with engine.begin() as connection:
        connection.execute(SqlSessionSpawn.__table__.insert(), _ROWS["session_spawns"])
    _migrate(database, _REVISION)
    with engine.connect() as connection:
        assert connection.execute(sa.select(SqlSessionSpawn.nonce)).scalar_one() == "2" * 32
        assert (
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalar_one()
            == _REVISION
        )
    scripts = ScriptDirectory("omnigent/db/migrations")
    assert scripts.get_revision("as1b2c3d4e5f").down_revision == "b4e2d7a91c35"
    assert scripts.get_revision("c6e9a2b7d481").down_revision == (
        "as1b2c3d4e5f",
        _PREVIOUS,
    )


@pytest.mark.parametrize("table_name", list(_TABLES))
def test_nonempty_table_blocks_downgrade_before_any_spawn_table_is_dropped(
    database, table_name
) -> None:
    _migrate(database, _REVISION)
    _, engine = database
    table = _TABLES[table_name]
    with engine.begin() as connection:
        connection.execute(table.insert(), _ROWS[table_name])
    with pytest.raises(RuntimeError, match=f"Cannot remove retained rows from {table_name}"):
        _migrate(database, _BRANCH_POINT, down=True)
    assert set(_TABLES) <= set(sa.inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert connection.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 1


def test_downgrade_to_previous_main_preserves_the_sibling_receipt_branch(database) -> None:
    _migrate(database, _REVISION)
    _, engine = database
    with engine.begin() as connection:
        connection.execute(SqlSessionSpawn.__table__.insert(), _ROWS["session_spawns"])

    _migrate(database, _PREVIOUS, down=True)

    assert set(_TABLES) <= set(sa.inspect(engine).get_table_names())
    with engine.connect() as connection:
        assert set(
            connection.execute(sa.text("SELECT version_num FROM alembic_version")).scalars()
        ) == {
            _PREVIOUS,
            "as1b2c3d4e5f",
        }
        assert connection.execute(sa.select(SqlSessionSpawn.nonce)).scalar_one() == "2" * 32


@pytest.mark.parametrize("column,value", [("state", 0), ("phase", 0), ("phase", 7)])
def test_reservation_rejects_unknown_lifecycle_codes(database, column, value) -> None:
    _migrate(database, _REVISION)
    _, engine = database
    row = {**_ROWS["session_spawns"], column: value}
    with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
        connection.execute(SqlSessionSpawn.__table__.insert(), row)


@pytest.mark.parametrize("column,limit", [("file_mapping", 7200), ("cleanup_blob_keys", 3600)])
def test_reservation_binary_columns_enforce_the_declared_stored_byte_cap(
    database, column, limit
) -> None:
    _migrate(database, _REVISION)
    _, engine = database
    row = {**_ROWS["session_spawns"], column: b"x" * limit}
    with engine.begin() as connection:
        connection.execute(SqlSessionSpawn.__table__.insert(), row)
    with pytest.raises(sa.exc.StatementError) as rejected, engine.begin() as connection:
        connection.execute(SqlSessionSpawn.__table__.update().values({column: b"x" * (limit + 1)}))
    assert isinstance(rejected.value.orig, ValueError)


@pytest.mark.parametrize("identity", ["child_id", "name_digest"])
@pytest.mark.parametrize("other_workspace", [False, True])
def test_reservation_unique_identities_are_scoped_to_the_workspace(
    database, identity, other_workspace
) -> None:
    _migrate(database, _REVISION)
    _, engine = database
    first = {**_ROWS["session_spawns"], "name_digest": b"n" * 32}
    second = {
        **first,
        "workspace_id": int(other_workspace),
        "nonce": "5" * 32,
        "child_id": "6" * 32,
        "name_digest": b"m" * 32,
        identity: first[identity],
    }
    with engine.begin() as connection:
        connection.execute(SqlSessionSpawn.__table__.insert(), first)
    if other_workspace:
        with engine.begin() as connection:
            connection.execute(SqlSessionSpawn.__table__.insert(), second)
    else:
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as connection:
            connection.execute(SqlSessionSpawn.__table__.insert(), second)


@pytest.mark.parametrize("dialect", ["mysql", "postgresql"])
def test_migration_ddl_uses_bounded_binary_types_without_defaults_or_foreign_keys(dialect) -> None:
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name=dialect, opts={"as_sql": True, "output_buffer": output}
    )
    with Operations.context(context):
        for module in (
            "as1b2c3d4e5f_add_pending_session_spawns",
            "e7a94c2d6b10_add_files_blob_reference_index",
        ):
            importlib.import_module(f"omnigent.db.migrations.versions.{module}").upgrade()
    ddl = output.getvalue()
    assert "DEFAULT " not in ddl
    assert "FOREIGN KEY" not in ddl
    assert "AUTO_INCREMENT" not in ddl
    assert "CREATE INDEX ix_files_blob_reference ON files (workspace_id, blob_key, id)" in ddl
    if dialect == "mysql":
        assert "file_mapping VARBINARY(7200)" in ddl
        assert "cleanup_blob_keys VARBINARY(3600)" in ddl
        assert "request_digest BINARY(32)" in ddl
        assert "parent_id BINARY(16)" in ddl
        assert " BLOB" not in ddl
    else:
        assert "file_mapping BYTEA" in ddl
        assert "request_digest BYTEA" in ddl


@pytest.mark.parametrize(
    "blob_key,referenced",
    [("a" * 32, True), ("b" * 32, True), ("c" * 32, False), ("e" * 32, False), ("f" * 32, False)],
    ids=[
        "shared-reference",
        "legacy-null-key",
        "id-now-references-another-blob",
        "unreferenced",
        "other-workspace-only",
    ],
)
def test_blob_reference_and_legacy_lookup_plans_use_indexed_searches(
    database, blob_key, referenced
) -> None:
    _migrate(database, _REVISION)
    _, engine = database
    rows = [
        {"workspace_id": 0, "id": "1" * 32, "blob_key": "a" * 32},
        {"workspace_id": 0, "id": "b" * 32, "blob_key": None},
        {"workspace_id": 0, "id": "c" * 32, "blob_key": "a" * 32},
        {"workspace_id": 1, "id": "2" * 32, "blob_key": "f" * 32},
    ]
    with engine.begin() as connection:
        connection.execute(
            SqlFile.__table__.insert(),
            [{**row, "created_at": 1, "filename": "example.txt", "bytes": 3} for row in rows],
        )
        statements = [
            sa.select(SqlFile.id)
            .where(SqlFile.workspace_id == 0, SqlFile.blob_key == blob_key)
            .limit(1),
            sa.select(SqlFile.id)
            .where(SqlFile.workspace_id == 0, SqlFile.id == blob_key, SqlFile.blob_key.is_(None))
            .limit(1),
        ]
        results = []
        for index, statement in enumerate(statements):
            compiled = statement.compile(engine)
            parameters = tuple(
                bytes.fromhex(value) if isinstance(value, str) else value
                for name in compiled.positiontup
                for value in (compiled.params[name],)
            )
            plan = " ".join(
                row[3]
                for row in connection.exec_driver_sql(
                    "EXPLAIN QUERY PLAN " + str(compiled), parameters
                )
            )
            assert "SEARCH files USING" in plan and "SCAN" not in plan, plan
            if index == 0:
                assert "ix_files_blob_reference" in plan, plan
            results.append(connection.execute(statement).scalar_one_or_none())
        assert any(result is not None for result in results) == referenced
