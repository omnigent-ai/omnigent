"""Add the ``memories`` table for long-term memory.

Schema only: a new table keyed by ``(workspace_id, actor_id, path)`` that nothing
reads or writes until the memory feature ships. Deployment and rollback order is in
``docs/memories-table-migration.md``.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARCHAR as MySQLVarchar
from sqlalchemy.dialects.postgresql import VARCHAR as PGVarchar

from omnigent.db.envelope import EnvelopedText

revision: str = "ffeec66eadb5"
down_revision: str | None = "a8be64286ffc"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "memories"
# Dialects that store a value of any type in any column.
_SQLITE_FAMILY = frozenset({"sqlite", "cloudflare_d1"})
# SQLite ignores declared lengths and types, so bound every column there: strings
# at up to four bytes per character, envelopes at their stored size.
_SQLITE_CHECKS = {
    "ck_memories_workspace_id_integer": "typeof(workspace_id) = 'integer'",
    "ck_memories_actor_id_bytes": "length(CAST(actor_id AS BLOB)) <= 640",
    "ck_memories_path_bytes": "length(CAST(path AS BLOB)) <= 2048",
    "ck_memories_content_bytes": "length(CAST(content AS BLOB)) <= 16383",
    "ck_memories_description_bytes": (
        "description IS NULL OR length(CAST(description AS BLOB)) <= 2100"
    ),
    "ck_memories_created_at_integer": "typeof(created_at) = 'integer'",
    "ck_memories_updated_at_integer": "typeof(updated_at) = 'integer'",
}


def _exact(length: int) -> sa.types.TypeEngine[str]:
    """A string compared byte for byte, without trailing-space padding."""
    return (
        sa.String(length)
        .with_variant(
            MySQLVarchar(length, charset="utf8mb4", collation="utf8mb4_0900_bin"), "mysql"
        )
        .with_variant(PGVarchar(length, collation="C"), "postgresql")
    )


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table(_TABLE):
        return
    checks = (
        [sa.CheckConstraint(sql, name=name) for name, sql in _SQLITE_CHECKS.items()]
        if bind.dialect.name in _SQLITE_FAMILY
        else []
    )
    op.create_table(
        _TABLE,
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "actor_id",
            sa.String(160).with_variant(
                MySQLVarchar(160, charset="utf8mb4", collation="utf8mb4_0900_bin"), "mysql"
            ),
            nullable=False,
        ),
        sa.Column("path", _exact(512), nullable=False),
        sa.Column("content", EnvelopedText(16_383), nullable=False),
        sa.Column("description", EnvelopedText(2_100), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "actor_id", "path"),
        *checks,
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
