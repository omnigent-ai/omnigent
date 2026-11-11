"""The label-key index is additive and reversible on existing label rows."""

from importlib import import_module

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_conversation_labels_key_index_preserves_existing_rows() -> None:
    migration = import_module(
        "omnigent.db.migrations.versions.b4e1c7d2a9f3_index_conversation_labels_by_key"
    )
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        connection.execute(
            sa.text(
                "CREATE TABLE conversation_labels (workspace_id INTEGER, conversation_id BLOB, "
                "key VARCHAR(128), value VARCHAR, updated_at INTEGER, "
                "PRIMARY KEY (workspace_id, conversation_id, key))"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO conversation_labels VALUES (0, X'01', 'omnigent.pinned.a', '1', 1)"
            )
        )
        migration.upgrade()
        indexes = sa.inspect(connection).get_indexes("conversation_labels")
        assert indexes == [
            {
                "name": "ix_conversation_labels_key",
                "column_names": ["workspace_id", "key", "conversation_id"],
                "unique": 0,
                "dialect_options": {},
            }
        ]
        plan = connection.execute(
            sa.text(
                "EXPLAIN QUERY PLAN SELECT conversation_id FROM conversation_labels "
                "WHERE workspace_id = 0 AND key IN ('omnigent.pinned.a', 'omnigent.pinned.b')"
            )
        ).all()
        assert "ix_conversation_labels_key" in str(plan)
        migration.downgrade()
        assert sa.inspect(connection).get_indexes("conversation_labels") == []
        assert (
            connection.execute(sa.text("SELECT count(*) FROM conversation_labels")).scalar() == 1
        )
    engine.dispose()
