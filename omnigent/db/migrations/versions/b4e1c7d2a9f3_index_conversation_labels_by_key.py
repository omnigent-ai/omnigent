"""Index conversation labels by key so the pinned-sessions list stays fast.

Revision ID: b4e1c7d2a9f3
Revises: a7d4c2e9f610

The primary key leads with ``conversation_id``, so a lookup by key alone (the
sidebar's ``pinned=true`` list) scanned every label in the workspace.

Deployment: additive; older application code ignores it. On PostgreSQL it
builds ``CONCURRENTLY`` so label writes continue during the build, and a
rerun keeps a valid index and rebuilds one a failed build left invalid.
"""

import sqlalchemy as sa
from alembic import op

revision = "b4e1c7d2a9f3"
down_revision = "a7d4c2e9f610"
branch_labels = None
depends_on = None

_INDEX = "ix_conversation_labels_key"
_TABLE = "conversation_labels"
_COLUMNS = ["workspace_id", "key", "conversation_id"]


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        op.create_index(_INDEX, _TABLE, _COLUMNS)
        return
    # CONCURRENTLY can't run in a transaction; autocommit lets writers proceed.
    with op.get_context().autocommit_block():
        # A failed concurrent build leaves an INVALID index that IF NOT EXISTS would keep.
        invalid = (
            op.get_bind()
            .execute(
                sa.text(
                    "SELECT i.indexrelid::regclass::text FROM pg_index i "
                    "JOIN pg_class c ON c.oid = i.indexrelid "
                    "WHERE i.indrelid = to_regclass(:table) AND c.relname = :name "
                    "AND NOT i.indisvalid"
                ),
                {"table": _TABLE, "name": _INDEX},
            )
            .scalar()
        )
        if invalid:
            op.execute(f"DROP INDEX CONCURRENTLY {invalid}")
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} ON {_TABLE} ({', '.join(_COLUMNS)})"
        )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
