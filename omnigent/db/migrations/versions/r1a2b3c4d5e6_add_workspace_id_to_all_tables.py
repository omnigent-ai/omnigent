"""Add workspace_id to every table and fold it into the primary key.

Revision ID: r1a2b3c4d5e6
Revises: q1a2b3c4d5e6
Create Date: 2026-07-07 00:00:00.000000

Adds a ``workspace_id`` tenant-partition column to all twelve tables and
extends each primary key to ``(workspace_id, <existing pk cols>)``.  The
column is NOT NULL with ``server_default = 0`` so existing rows backfill
to workspace 0 (the single-workspace / unassigned sentinel) and inserts
that omit it land in workspace 0.  ``workspace_id`` leads the composite
key so rows for one workspace stay contiguous for prefix scans.

There are no FK constraints in the schema anymore (see ``p1a2b3c4d5e6``),
so rebuilding each primary key is a purely local operation per table.

SQLite note: ``batch_alter_table(recreate="always")`` rebuilds the table
so the primary key can change (SQLite cannot alter a PK in place); the
new ``create_primary_key`` overrides the reflected single-column PK.  On
PostgreSQL the existing named PK is dropped explicitly first (a table can
hold only one primary key) before the wider one is added.  Both paths
guard the rebuilds with ``PRAGMA foreign_keys`` on SQLite.
"""

from __future__ import annotations

import contextlib
import warnings
from collections.abc import Iterator, Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "r1a2b3c4d5e6"
down_revision: str | None = "q1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every table mapped to the primary-key columns it had before this
# migration. The new primary key is ``["workspace_id", *existing]``.
_TABLE_PKS: dict[str, list[str]] = {
    "agents": ["id"],
    "files": ["id"],
    "users": ["id"],
    "account_tokens": ["id"],
    "session_permissions": ["user_id", "conversation_id"],
    "conversations": ["id"],
    "conversation_items": ["id"],
    "conversation_labels": ["conversation_id", "key"],
    "comments": ["id"],
    "policies": ["id"],
    "hosts": ["owner", "name"],
    "user_daily_cost": ["user_id", "day_utc"],
}


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def _existing_pk_name(table: str) -> str | None:
    """Reflect the current primary-key constraint name (PostgreSQL path)."""
    return sa.inspect(op.get_bind()).get_pk_constraint(table).get("name")


@contextlib.contextmanager
def _quiet_pk_override() -> Iterator[None]:
    """
    Silence the expected SQLite batch-rebuild warning about the reflected
    single-column PK not matching the wider one we install. The override is
    intentional here, and this fires once per table on every fresh DB.
    """
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message=r".*not matching locally specified columns.*",
            category=sa.exc.SAWarning,
        )
        yield


def upgrade() -> None:
    """Add ``workspace_id`` and widen every primary key to include it."""
    sqlite = _is_sqlite()
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    is_mysql = op.get_bind().dialect.name == "mysql"
    for table, pk_cols in _TABLE_PKS.items():
        if is_mysql:
            # Use raw DDL on MySQL to avoid batch_alter_table reading ORM
            # metadata and trying to apply server_defaults (e.g. '' on title)
            # that MySQL rejects on TEXT/BLOB columns.
            pk_col_list = ", ".join(f"`{c}`" for c in ["workspace_id", *pk_cols])
            op.execute(
                sa.text(
                    f"ALTER TABLE `{table}` "
                    f"ADD COLUMN workspace_id BIGINT NOT NULL DEFAULT 0 FIRST, "
                    f"DROP PRIMARY KEY, "
                    f"ADD CONSTRAINT `pk_{table}` PRIMARY KEY ({pk_col_list})"
                )
            )
            continue
        # On PostgreSQL the current PK must be dropped before a wider one
        # can be added; on SQLite the batch rebuild overrides it in place.
        old_pk_name = None if sqlite else _existing_pk_name(table)
        with (
            _quiet_pk_override(),
            op.batch_alter_table(table, recreate="always" if sqlite else "auto") as batch_op,
        ):
            batch_op.add_column(
                sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0")
            )
            if old_pk_name is not None:
                batch_op.drop_constraint(old_pk_name, type_="primary")
            batch_op.create_primary_key(f"pk_{table}", ["workspace_id", *pk_cols])

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))


def downgrade() -> None:
    """Restore each original primary key and drop ``workspace_id``."""
    sqlite = _is_sqlite()
    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = OFF"))

    is_mysql = op.get_bind().dialect.name == "mysql"
    for table, pk_cols in _TABLE_PKS.items():
        if is_mysql:
            pk_col_list = ", ".join(f"`{c}`" for c in pk_cols)
            op.execute(
                sa.text(
                    f"ALTER TABLE `{table}` "
                    f"DROP PRIMARY KEY, "
                    f"DROP COLUMN workspace_id, "
                    f"ADD CONSTRAINT `pk_{table}` PRIMARY KEY ({pk_col_list})"
                )
            )
            continue
        old_pk_name = None if sqlite else _existing_pk_name(table)
        with (
            _quiet_pk_override(),
            op.batch_alter_table(table, recreate="always" if sqlite else "auto") as batch_op,
        ):
            if old_pk_name is not None:
                batch_op.drop_constraint(old_pk_name, type_="primary")
            batch_op.drop_column("workspace_id")
            batch_op.create_primary_key(f"pk_{table}", pk_cols)

    if sqlite:
        op.execute(sa.text("PRAGMA foreign_keys = ON"))
