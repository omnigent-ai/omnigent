"""Index the sessions bound to a host for managed startup recovery.

Deployment: additive; older application code ignores it. On PostgreSQL it
builds ``CONCURRENTLY`` so session writes continue during the build, and a
rerun keeps a valid index and rebuilds one a failed build left invalid.
"""

import sqlalchemy as sa
from alembic import op

revision = "nf12ab34cd56"
down_revision = "mm1a2b3c4d5e"
branch_labels = None
depends_on = None

_INDEX = "ix_conversation_metadata_host_id"
_TABLE = "omnigent_conversation_metadata"
_COLUMNS = ["workspace_id", "host_id", "id"]


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        op.create_index(_INDEX, _TABLE, _COLUMNS)
        return
    # CONCURRENTLY can't run in a transaction; autocommit lets writers proceed.
    with op.get_context().autocommit_block():
        # A failed concurrent build leaves an INVALID index that IF NOT EXISTS would keep.
        # Match it only on the table this connection resolves, never a same-named index elsewhere.
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
