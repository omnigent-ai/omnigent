"""Unify the session-owner identity columns to ``user_id``.

Revision ID: b3c1a2d4e5f6
Revises: f82e866d9de0
Create Date: 2026-07-21 18:00:00.000000

Two tables named the same session-owner identity differently from the
schema-wide ``user_id`` convention (``session_permissions.user_id``,
``account_tokens.user_id``, ``device_grants.user_id``):

- ``hosts.owner`` (``VARCHAR(256)``) → ``hosts.user_id`` (``VARCHAR(128)``).
  Narrowing is safe: the value is a Databricks user identity (email) or the
  reserved ``"local"`` user, both far under 128. The
  ``uq_hosts_workspace_owner_name`` unique constraint is renamed to
  ``uq_hosts_workspace_user_id_name`` (same columns, ``owner`` → ``user_id``).
- ``scheduled_tasks.owner_user_id`` → ``scheduled_tasks.user_id`` (type
  unchanged, ``VARCHAR(128)``). The ``ix_scheduled_tasks_owner_user_id`` index
  is renamed to ``ix_scheduled_tasks_user_id`` (same columns).

``user_daily_cost.user_id`` already matches the convention and is untouched.

Dialect strategy
----------------
- **SQLite**: cannot rename/retype a column in place; ``batch_alter_table``
  with ``recreate="always"`` rebuilds each table with the new column name,
  type, and constraint/index names.
- **PostgreSQL / MySQL**: native ``ALTER TABLE`` DDL (``recreate="auto"``) —
  ``RENAME COLUMN`` + ``ALTER COLUMN TYPE`` + constraint/index swap, no copy.

No PRAGMA foreign_keys guard needed — all FK constraints were removed in
p1a2b3c4d5e6.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b3c1a2d4e5f6"
down_revision: str | None = "f82e866d9de0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _is_sqlite() -> bool:
    return op.get_bind().dialect.name == "sqlite"


def upgrade() -> None:
    """Rename owner/owner_user_id → user_id (and narrow hosts.user_id to 128).

    Each rename is split across batches: a single ``batch_alter_table`` that both
    renames a column *and* drops/creates a constraint or index referencing it
    trips Alembic's batch reflection (it maps the reflected object onto the
    not-yet-renamed column). So drop the dependent object first, rename in its
    own batch, then create the renamed object.
    """
    recreate = "always" if _is_sqlite() else "auto"

    # hosts.owner → user_id, VARCHAR(256) → VARCHAR(128). The unique constraint
    # sits on the renamed column, and SQLite can only drop a constraint via a
    # table rebuild, so each step is its own batch.
    with op.batch_alter_table("hosts", recreate=recreate) as batch_op:
        batch_op.drop_constraint("uq_hosts_workspace_owner_name", type_="unique")
    with op.batch_alter_table("hosts", recreate=recreate) as batch_op:
        # existing_type required by MySQL for CHANGE/MODIFY COLUMN.
        batch_op.alter_column(
            "owner",
            new_column_name="user_id",
            existing_type=sa.String(256),
            type_=sa.String(128),
            existing_nullable=False,
        )
    with op.batch_alter_table("hosts", recreate=recreate) as batch_op:
        batch_op.create_unique_constraint(
            "uq_hosts_workspace_user_id_name", ["workspace_id", "user_id", "name"]
        )

    # scheduled_tasks.owner_user_id → user_id. The index is droppable outside a
    # batch on every dialect, so no table rebuild is needed to remove it.
    op.drop_index("ix_scheduled_tasks_owner_user_id", table_name="scheduled_tasks")
    with op.batch_alter_table("scheduled_tasks", recreate=recreate) as batch_op:
        batch_op.alter_column(
            "owner_user_id",
            new_column_name="user_id",
            existing_type=sa.String(128),
            existing_nullable=True,
        )
    op.create_index(
        "ix_scheduled_tasks_user_id", "scheduled_tasks", ["workspace_id", "user_id", "id"]
    )


def downgrade() -> None:
    """Restore owner / owner_user_id column names (and hosts width to 256)."""
    recreate = "always" if _is_sqlite() else "auto"

    op.drop_index("ix_scheduled_tasks_user_id", table_name="scheduled_tasks")
    with op.batch_alter_table("scheduled_tasks", recreate=recreate) as batch_op:
        batch_op.alter_column(
            "user_id",
            new_column_name="owner_user_id",
            existing_type=sa.String(128),
            existing_nullable=True,
        )
    op.create_index(
        "ix_scheduled_tasks_owner_user_id",
        "scheduled_tasks",
        ["workspace_id", "owner_user_id", "id"],
    )

    with op.batch_alter_table("hosts", recreate=recreate) as batch_op:
        batch_op.drop_constraint("uq_hosts_workspace_user_id_name", type_="unique")
    with op.batch_alter_table("hosts", recreate=recreate) as batch_op:
        batch_op.alter_column(
            "user_id",
            new_column_name="owner",
            existing_type=sa.String(128),
            type_=sa.String(256),
            existing_nullable=False,
        )
    with op.batch_alter_table("hosts", recreate=recreate) as batch_op:
        batch_op.create_unique_constraint(
            "uq_hosts_workspace_owner_name", ["workspace_id", "owner", "name"]
        )
