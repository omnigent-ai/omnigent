"""Add the ``memories`` table for long-term memory.

Schema only: a new table keyed by ``(workspace_id, actor_type, actor_id, path)``
that nothing reads or writes until the memory feature ships. This migration runs
online.

Deploy this revision before the feature, which stays off until a server sets
``OMNIGENT_FEATURES=memory``. The feature release can roll back to this release
at any time: the table stays and nothing reads it, and turning the flag off has
the same effect without a deploy. A binary older than this revision rejects it at
startup, so it is not a rollback target once a database has migrated.

The downgrade drops the table and every stored memory. It is for development
recovery; a production removal follows the staged procedure in
``docs/DATABASE_BEST_PRACTICES.md``.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARBINARY
from sqlalchemy.dialects.mysql import VARCHAR as MySQLVarchar
from sqlalchemy.dialects.postgresql import VARCHAR as PGVarchar

revision: str = "ffeec66eadb5"
down_revision: str | None = "f7c2d8a4b901"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "memories"
# Dialects that store a value of any type in any column.
_SQLITE_FAMILY = frozenset({"sqlite", "cloudflare_d1"})
# SQLite ignores declared lengths and types, so bound every column there: strings
# at up to four bytes per character, envelopes at their stored size.
_SQLITE_CHECKS = {
    "ck_memories_workspace_id_integer": "typeof(workspace_id) = 'integer'",
    "ck_memories_actor_id_bytes": "length(CAST(actor_id AS BLOB)) <= 512",
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


def _binary(length: int) -> sa.types.TypeEngine[bytes]:
    """
    Bounded binary storage for an envelope column.

    The model declares these columns with the application's ``EnvelopedText``;
    the migration spells out the stored type so later changes to that class
    cannot change what this revision creates.
    """
    return sa.LargeBinary().with_variant(VARBINARY(length), "mysql")


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
        sa.Column("actor_type", sa.SmallInteger(), nullable=False),
        sa.Column(
            "actor_id",
            sa.String(128).with_variant(
                MySQLVarchar(128, charset="utf8mb4", collation="utf8mb4_0900_bin"), "mysql"
            ),
            nullable=False,
        ),
        sa.Column("path", _exact(512), nullable=False),
        sa.Column("content", _binary(16_383), nullable=False),
        sa.Column("description", _binary(2_100), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "actor_type", "actor_id", "path"),
        # Actor type codes: 1 = user.
        sa.CheckConstraint("actor_type IN (1)", name="ck_memories_actor_type"),
        *checks,
    )


def downgrade() -> None:
    op.drop_table(_TABLE)
