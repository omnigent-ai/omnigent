"""Add nullable sibling-family membership to conversation metadata.

Deploy before family readers and writers. This schema-aware release is the
application rollback floor: pre-migration binaries reject the new Alembic head
and require a schema downgrade, which discards family membership. PostgreSQL
builds the index concurrently so session writes continue.
"""

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "f1c7a9e2b406"
down_revision: str | None = "d0a1f879a01d"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


_TABLE = "omnigent_conversation_metadata"
_INDEX = "ix_conversation_metadata_family_id"
_COLUMNS = ["workspace_id", "family_id", "id"]


def upgrade() -> None:
    # A concurrent index build commits the column before it can fail.
    if "family_id" not in {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}:
        op.add_column(_TABLE, sa.Column("family_id", Uuid16(), nullable=True))
    if op.get_bind().dialect.name != "postgresql":
        if not sa.inspect(op.get_bind()).has_index(_TABLE, _INDEX):
            op.create_index(_INDEX, _TABLE, _COLUMNS)
        return
    with op.get_context().autocommit_block():
        # Rebuild only an invalid index on this table, including after a failed attempt.
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
    op.drop_index(
        "ix_conversation_metadata_family_id", table_name="omnigent_conversation_metadata"
    )
    with op.batch_alter_table("omnigent_conversation_metadata") as batch:
        batch.drop_column("family_id")
