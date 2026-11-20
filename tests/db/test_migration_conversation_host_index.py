"""The startup host lookup adds only a reversible index to existing metadata."""

from importlib import import_module

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_conversation_host_index_preserves_existing_rows() -> None:
    migration = import_module(
        "omnigent.db.migrations.versions.nf12ab34cd56_index_conversations_by_host"
    )
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        connection.execute(
            sa.text(
                "CREATE TABLE omnigent_conversation_metadata "
                "(workspace_id INTEGER, id BLOB, host_id BLOB, PRIMARY KEY (workspace_id, id))"
            )
        )
        connection.execute(
            sa.text("INSERT INTO omnigent_conversation_metadata VALUES (0, X'01', X'02')")
        )
        migration.upgrade()
        assert sa.inspect(connection).get_table_names() == ["omnigent_conversation_metadata"]
        indexes = sa.inspect(connection).get_indexes("omnigent_conversation_metadata")
        assert indexes == [
            {
                "name": "ix_conversation_metadata_host_id",
                "column_names": ["workspace_id", "host_id", "id"],
                "unique": 0,
                "dialect_options": {},
            }
        ]
        migration.downgrade()
        assert sa.inspect(connection).get_indexes("omnigent_conversation_metadata") == []
        assert (
            connection.execute(
                sa.text("SELECT count(*) FROM omnigent_conversation_metadata")
            ).scalar()
            == 1
        )
    engine.dispose()
