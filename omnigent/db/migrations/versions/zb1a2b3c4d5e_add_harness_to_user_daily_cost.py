"""add harness column to user_daily_cost

Revision ID: zb1a2b3c4d5e
Revises: ll1a2b3c4d5e
Create Date: 2026-08-21 00:00:00.000000

Adds a ``harness`` column to ``user_daily_cost`` to enable optional per-harness
budget scoping. The table continues to store daily cost records only
(``day_utc`` as ``"YYYY-MM-DD"``). Period-based policies (week, month, quarter,
year) aggregate daily records at read time rather than pre-computing rollups.

Supports two budget modes:
- **Cross-harness budgets** (default): Use the sentinel value ``"__all__"`` to
  sum cost across all harnesses for a user+day.
- **Per-harness budgets**: Track cost separately for each harness (e.g.
  ``"codex-native"``).

This is a **backward-compatible** additive migration:
- Existing daily-cost rows get ``harness="__all__"`` via the server default
- Existing queries that don't filter by harness will read all rows (cross-harness)
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "zb1a2b3c4d5e"
down_revision: str | None = "ll1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _existing_pk_name(table: str) -> str | None:
    """Reflect the current primary-key constraint name."""
    constraint = sa.inspect(op.get_bind()).get_pk_constraint(table)
    return constraint.get("name") if constraint else None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    """
    Add harness column to user_daily_cost and rebuild primary key.

    Uses ALTER TABLE for better performance and less downtime. The new
    harness column defaults to "__all__" for existing rows.
    """
    sqlite = _is_sqlite()
    dialect = op.get_bind().dialect.name

    if dialect == "mysql":
        # MySQL: use raw DDL
        op.execute(
            sa.text(
                "ALTER TABLE `user_daily_cost` "
                "ADD COLUMN harness VARCHAR(64) NOT NULL DEFAULT '__all__' AFTER day_utc, "
                "DROP PRIMARY KEY, "
                "ADD CONSTRAINT `pk_user_daily_cost` "
                "PRIMARY KEY (workspace_id, user_id, day_utc, harness)"
            )
        )
    elif dialect == "cockroachdb":
        # CockroachDB: cannot do PK change with other schema changes in same transaction
        # First add the column
        bind = op.get_bind()
        bind.execute(
            sa.text(
                "ALTER TABLE user_daily_cost "
                "ADD COLUMN harness VARCHAR(64) NOT NULL DEFAULT '__all__'"
            )
        )
        # Publish schema changes at commit
        bind.commit()
        bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
        # Then rebuild PK with the new column
        old_pk_name = _existing_pk_name("user_daily_cost")
        if old_pk_name:
            bind.execute(
                sa.text(
                    f"ALTER TABLE user_daily_cost "
                    f'DROP CONSTRAINT "{old_pk_name}", '
                    f"ADD CONSTRAINT pk_user_daily_cost "
                    f"PRIMARY KEY (workspace_id, user_id, day_utc, harness)"
                )
            )
    else:
        # PostgreSQL/SQLite: use batch_alter_table
        old_pk_name = None if sqlite else _existing_pk_name("user_daily_cost")
        with op.batch_alter_table(
            "user_daily_cost", recreate="always" if sqlite else "auto"
        ) as batch_op:
            # Add harness column with default value
            batch_op.add_column(
                sa.Column("harness", sa.String(64), nullable=False, server_default="__all__")
            )
            # Drop old primary key if it exists
            if old_pk_name is not None:
                batch_op.drop_constraint(old_pk_name, type_="primary")
            # Add new primary key including harness
            batch_op.create_primary_key(
                "pk_user_daily_cost", ["workspace_id", "user_id", "day_utc", "harness"]
            )


def downgrade() -> None:
    """
    Remove harness column and restore original primary key.

    WARNING: This will DELETE all per-harness rows (harness != "__all__").
    Only cross-harness data will be preserved.
    """
    sqlite = _is_sqlite()
    dialect = op.get_bind().dialect.name

    # Delete per-harness rows before removing the column (if column exists)
    # For CockroachDB, check if column exists first
    if dialect == "cockroachdb":
        bind = op.get_bind()
        columns = {col["name"] for col in sa.inspect(bind).get_columns("user_daily_cost")}
        if "harness" in columns:
            bind.execute(sa.text("DELETE FROM user_daily_cost WHERE harness != '__all__'"))
    else:
        # Other databases: assume column exists
        op.execute(sa.text("DELETE FROM user_daily_cost WHERE harness != '__all__'"))

    if dialect == "mysql":
        # MySQL: use raw DDL
        op.execute(
            sa.text(
                "ALTER TABLE `user_daily_cost` "
                "DROP PRIMARY KEY, "
                "DROP COLUMN harness, "
                "ADD CONSTRAINT `pk_user_daily_cost` "
                "PRIMARY KEY (workspace_id, user_id, day_utc)"
            )
        )
    elif dialect == "cockroachdb":
        # CockroachDB: publish schema changes via commit, then drop column
        bind = op.get_bind()
        columns = {col["name"] for col in sa.inspect(bind).get_columns("user_daily_cost")}

        # Only proceed if harness column exists
        if "harness" in columns:
            # Get the actual PK name (may vary across database states)
            old_pk_name = _existing_pk_name("user_daily_cost")
            if old_pk_name:
                # First rebuild the PK without harness
                bind.execute(
                    sa.text(
                        f"ALTER TABLE user_daily_cost "
                        f'DROP CONSTRAINT "{old_pk_name}", '
                        f"ADD CONSTRAINT pk_user_daily_cost "
                        f"PRIMARY KEY (workspace_id, user_id, day_utc)"
                    )
                )
                # CRDB publishes schema changes at commit
                bind.commit()
                bind.execute(sa.text("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"))
            # Then drop the harness column
            bind.execute(sa.text("ALTER TABLE user_daily_cost DROP COLUMN harness"))
    else:
        # PostgreSQL/SQLite: use batch_alter_table
        old_pk_name = None if sqlite else _existing_pk_name("user_daily_cost")
        with op.batch_alter_table(
            "user_daily_cost", recreate="always" if sqlite else "auto"
        ) as batch_op:
            # Drop current primary key if it exists
            if old_pk_name is not None:
                batch_op.drop_constraint(old_pk_name, type_="primary")
            # Drop harness column
            batch_op.drop_column("harness")
            # Recreate original primary key without harness
            batch_op.create_primary_key(
                "pk_user_daily_cost", ["workspace_id", "user_id", "day_utc"]
            )
