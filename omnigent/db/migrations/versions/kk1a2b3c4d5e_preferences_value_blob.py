"""Limit stored preference values to 65,535 bytes.

Oversized rows are discarded so preference reads fall back to defaults.
Remaining bytes, including legacy uncompressed values, are unchanged.
Downgrade widens MySQL storage but cannot restore discarded preferences.

Stop old application instances before upgrading, or enforce strict SQL mode
on their MySQL connections. This migration's strict mode protects its ALTER;
old non-strict writers could still truncate oversized saves after it finishes.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import MEDIUMBLOB

revision: str = "kk1a2b3c4d5e"
down_revision: str | None = "jj1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PREFERENCES = sa.table("preferences", sa.column("value", sa.LargeBinary()))


def upgrade() -> None:
    dialect = op.get_bind().dialect.name
    value = _PREFERENCES.c.value
    # SQLite may still contain legacy TEXT; count UTF-8 bytes, not characters.
    size = (
        sa.func.length(sa.cast(value, sa.LargeBinary()))
        if dialect == "sqlite"
        else sa.func.octet_length(value)
    )
    # This table has only stored project ordering, so values above the BLOB limit
    # are not expected in practice. Deleting any outliers is acceptable because
    # affected users simply return to the default ordering.
    op.execute(_PREFERENCES.delete().where(size > 65_535))
    if dialect == "mysql":
        # An old writer racing the cleanup must fail the ALTER, never truncate a value.
        op.execute("SET @omnigent_preferences_sql_mode = @@SESSION.sql_mode")
        try:
            op.execute(
                "SET SESSION sql_mode = CONCAT_WS(',', "
                "NULLIF(@@SESSION.sql_mode, ''), 'STRICT_ALL_TABLES')"
            )
            with op.batch_alter_table("preferences") as batch:
                batch.alter_column(
                    "value",
                    existing_type=MEDIUMBLOB(),
                    type_=sa.LargeBinary(),
                    existing_nullable=False,
                )
        finally:
            op.execute("SET SESSION sql_mode = @omnigent_preferences_sql_mode")


def downgrade() -> None:
    if op.get_bind().dialect.name == "mysql":
        with op.batch_alter_table("preferences") as batch:
            batch.alter_column(
                "value",
                existing_type=sa.LargeBinary(),
                type_=MEDIUMBLOB(),
                existing_nullable=False,
            )
