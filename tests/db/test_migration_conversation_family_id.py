"""Family membership is nullable, tenant-scoped, and indexed for bounded reads."""

from importlib import import_module

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from omnigent.db.db_models import Uuid16


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
        connection.execute(table.insert(), original)
        before = connection.execute(sa.select(table)).all()
        migration.upgrade()
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
