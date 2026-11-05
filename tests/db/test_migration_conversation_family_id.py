"""Family membership is nullable, tenant-scoped, and indexed for bounded reads."""

from collections.abc import Callable
from importlib import import_module
from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.db_models import SqlConversationMetadata, Uuid16


def test_family_id_migration_preserves_metadata_and_indexes_membership() -> None:
    migration = import_module(
        "omnigent.db.migrations.versions.f1c7a9e2b406_add_conversation_family_id"
    )
    table = sa.Table(
        "omnigent_conversation_metadata",
        sa.MetaData(),
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("id", Uuid16(), primary_key=True),
        sa.Column("project_id", Uuid16(), nullable=True),
    )
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        table.create(connection)
        original = [
            {"workspace_id": workspace, "id": identifier * 32, "project_id": "f" * 32}
            for workspace, identifier in ((0, "1"), (0, "2"), (0, "3"), (1, "1"))
        ]
        original.append({"workspace_id": 1, "id": "1f" * 16, "project_id": "f" * 32})
        connection.execute(table.insert(), original)
        before = connection.execute(sa.select(table)).all()
        migration.upgrade()
        migration.upgrade()  # DDL may commit before the revision stamp on an interrupted upgrade.
        columns = {
            column["name"]: column for column in sa.inspect(connection).get_columns(table.name)
        }
        assert columns["family_id"]["nullable"] is True
        assert columns["family_id"]["default"] is None
        indexes = sa.inspect(connection).get_indexes(table.name)
        assert indexes[0]["column_names"] == ["workspace_id", "family_id", "id"]
        assert connection.exec_driver_sql(
            "SELECT COUNT(*) FROM omnigent_conversation_metadata WHERE family_id IS NULL"
        ).scalar_one() == len(original)

        family = bytes.fromhex("a" * 32)
        connection.exec_driver_sql(
            "UPDATE omnigent_conversation_metadata SET family_id = ? WHERE id != ?",
            (family, bytes.fromhex("3" * 32)),
        )
        query = (
            "SELECT id FROM omnigent_conversation_metadata "
            "WHERE workspace_id = ? AND family_id = ? AND id > ? ORDER BY id LIMIT 1"
        )
        params = (0, family, bytes.fromhex("1" * 32))
        assert connection.exec_driver_sql(query, params).scalars().all() == [
            bytes.fromhex("2" * 32)
        ]
        plan = connection.exec_driver_sql("EXPLAIN QUERY PLAN " + query, params).all()
        assert "COVERING INDEX ix_conversation_metadata_family_id" in str(plan)
        assert "id>?" in str(plan)

        # Older writers can still insert a standalone session after the schema deploy.
        connection.execute(table.insert(), {"workspace_id": 0, "id": "4" * 32})
        assert (
            connection.exec_driver_sql(
                "SELECT family_id FROM omnigent_conversation_metadata "
                "WHERE workspace_id = 0 AND id = ?",
                (bytes.fromhex("4" * 32),),
            ).scalar_one()
            is None
        )
        migration.downgrade()
        assert "family_id" not in {
            column["name"] for column in sa.inspect(connection).get_columns(table.name)
        }
        assert sa.inspect(connection).get_indexes(table.name) == []
        assert connection.execute(sa.select(table).where(table.c.id != "4" * 32)).all() == before
    engine.dispose()


def test_family_schema_bootstrap_and_upgrade_agree(db_uri: str) -> None:
    from omnigent.db.cockroachdb import _crdb_server_version, _prepare_crdb_schema_transaction
    from omnigent.db.utils import (
        _build_alembic_config,
        _get_current_db_revision,
        get_or_create_engine,
    )

    engine = get_or_create_engine(db_uri)
    original_revision = _get_current_db_revision(engine)
    migration = import_module(
        "omnigent.db.migrations.versions.f1c7a9e2b406_add_conversation_family_id"
    )
    table = "omnigent_conversation_metadata"
    index = "ix_conversation_metadata_family_id"

    def schema():
        inspector = sa.inspect(engine)
        column = next(c for c in inspector.get_columns(table) if c["name"] == "family_id")
        family_index = next(i for i in inspector.get_indexes(table) if i["name"] == index)
        return (
            type(column["type"].as_generic()),
            column["type"].length if engine.dialect.name == "mysql" else None,
            column["nullable"],
            column["default"],
            family_index["column_names"],
            bool(family_index["unique"]),
        )

    before = schema()
    orm_table = SqlConversationMetadata.__table__
    orm_column = orm_table.c.family_id
    orm_index = next(i for i in orm_table.indexes if i.name == index)
    assert before == (
        type(orm_column.type.load_dialect_impl(engine.dialect).as_generic()),
        16 if engine.dialect.name == "mysql" else None,
        orm_column.nullable,
        orm_column.server_default,
        list(orm_index.columns.keys()),
        orm_index.unique,
    )

    def migrate(operation: Callable[[], None]) -> None:
        with engine.connect() as connection:
            if engine.dialect.name == "cockroachdb":
                _prepare_crdb_schema_transaction(connection, _crdb_server_version(engine))
            context = MigrationContext.configure(connection)
            with Operations.context(context), context.begin_transaction():
                operation()
            connection.commit()

    identifier = uuid4().hex
    row_query = sa.select(orm_table).where(orm_table.c.id == identifier)
    with engine.begin() as connection:
        connection.execute(
            orm_table.insert(), {"id": identifier, "project_id": "f" * 32, "task_summary": "kept"}
        )
        original = connection.execute(row_query).one()
    try:
        migrate(migration.downgrade)
        migrate(migration.upgrade)
        if engine.dialect.name == "postgresql":
            with engine.connect() as connection:
                # Simulate a failed concurrent build after the column was committed.
                connection.execute(
                    sa.text(
                        "UPDATE pg_index SET indisvalid = false "
                        "WHERE indexrelid = to_regclass(:name)"
                    ),
                    {"name": index},
                )
                connection.commit()
                context = MigrationContext.configure(connection)
                with Operations.context(context), context.begin_transaction():
                    migration.upgrade()
                assert connection.execute(
                    sa.text(
                        "SELECT indisvalid FROM pg_index WHERE indexrelid = to_regclass(:name)"
                    ),
                    {"name": index},
                ).scalar_one()
            for missing_index in (True, False):
                # Committed DDL can survive without the Alembic revision stamp.
                with engine.begin() as connection:
                    if missing_index:
                        connection.exec_driver_sql(f"DROP INDEX {index}")
                    connection.execute(
                        sa.text("UPDATE alembic_version SET version_num = :revision"),
                        {"revision": migration.down_revision},
                    )
                restarted = sa.create_engine(db_uri)
                try:
                    # Later revisions are already applied in the shared test database.
                    command.upgrade(_build_alembic_config(db_uri), migration.revision)
                    with restarted.connect() as connection:
                        assert (
                            connection.exec_driver_sql(
                                "SELECT version_num FROM alembic_version"
                            ).scalar_one()
                            == migration.revision
                        )
                        assert connection.execute(
                            sa.text(
                                "SELECT indisvalid FROM pg_index "
                                "WHERE indexrelid = to_regclass(:name)"
                            ),
                            {"name": index},
                        ).scalar_one()
                        assert connection.execute(row_query).one() == original
                finally:
                    restarted.dispose()
        assert schema() == before
        assert before[2:] == (True, None, ["workspace_id", "family_id", "id"], False)
        with engine.connect() as connection:
            assert connection.execute(row_query).one() == original
    finally:
        if (
            engine.dialect.name == "postgresql"
            or "family_id" not in {c["name"] for c in sa.inspect(engine).get_columns(table)}
            or not sa.inspect(engine).has_index(table, index)
        ):
            migrate(migration.upgrade)
        if engine.dialect.name == "postgresql":
            with engine.begin() as connection:
                connection.execute(
                    sa.text("UPDATE alembic_version SET version_num = :revision"),
                    {"revision": original_revision},
                )
        with engine.begin() as connection:
            connection.execute(orm_table.delete().where(orm_table.c.id == identifier))
