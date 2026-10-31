"""Family lookup is indexed and the additive migration preserves labels."""

from importlib import import_module

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def test_label_lookup_index_preserves_rows_and_bounds_pagination() -> None:
    migration = import_module(
        "omnigent.db.migrations.versions.e7c3a1b9f025_index_conversation_labels_lookup"
    )
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection, Operations.context(MigrationContext.configure(connection)):
        connection.exec_driver_sql(
            "CREATE TABLE conversation_labels (workspace_id INTEGER, conversation_id BLOB, "
            "key TEXT, value TEXT, PRIMARY KEY (workspace_id, conversation_id, key))"
        )
        connection.exec_driver_sql(
            "INSERT INTO conversation_labels VALUES (0, X'01', 'family', 'a')"
        )
        migration.upgrade()
        indexes = sa.inspect(connection).get_indexes("conversation_labels")
        assert indexes[0]["column_names"] == ["workspace_id", "key", "value", "conversation_id"]
        plan = connection.exec_driver_sql(
            "EXPLAIN QUERY PLAN SELECT conversation_id FROM conversation_labels "
            "WHERE workspace_id=0 AND key='family' AND value='a' AND conversation_id>X'00' "
            "ORDER BY conversation_id LIMIT 31"
        ).all()
        assert "COVERING INDEX ix_conversation_labels_lookup" in str(plan)
        assert "conversation_id>?" in str(plan)
        migration.downgrade()
        assert sa.inspect(connection).get_indexes("conversation_labels") == []
        assert connection.exec_driver_sql("SELECT value FROM conversation_labels").scalar() == "a"
    engine.dispose()
