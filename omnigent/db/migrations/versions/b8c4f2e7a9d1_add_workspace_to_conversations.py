"""add workspace column to conversations

Revision ID: b8c4f2e7a9d1
Revises: a7b3c9d1e5f2
Create Date: 2026-05-29 12:00:00.000000

Adds ``conversations.workspace``: the absolute path on disk where
the runner should start (designs/SESSION_WORKSPACE_SELECTION.md).
Required when ``host_id`` is set; optional for CLI-launched sessions
that record their starting cwd for display purposes.

Also adds the ``host_id`` -> ``hosts.host_id`` foreign key
(ON DELETE SET NULL) and the ``ix_conversations_host_id`` index,
folded into this migration's batch rebuild of ``conversations`` so
the column added in a7b3c9d1e5f2 isn't left FK-less and unindexed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b8c4f2e7a9d1"
down_revision: str | None = "a7b3c9d1e5f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """
    Add the ``workspace`` column to ``conversations``.

    The column is nullable: existing rows have no workspace and
    pre-date this feature. New host-launched sessions must populate
    it (enforced by the check constraint
    ``ck_conversations_workspace_required_for_host``); CLI sessions
    may populate it for display but are not required to.

    Batch mode is required for SQLite, which can't ALTER constraints
    in place — alembic copies the table, applies the column + check
    in one shot, and renames it back.
    """
    bind = op.get_bind()
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("workspace", sa.String(length=2048), nullable=True))
        if bind.dialect.name not in ("mysql", "mariadb"):
            # MySQL/MariaDB: Alembic's batch_alter_table emits a CHECK
            # constraint syntax that MariaDB 11 rejects (error 1901).
            # The constraint is a data-integrity guard; the application
            # enforces it, so skipping it here is safe.
            batch_op.create_check_constraint(
                "ck_conversations_workspace_required_for_host",
                "host_id IS NULL OR workspace IS NOT NULL",
            )
        # Index + FK on host_id, folded into this batch since it already
        # recreates the table (avoids a second rebuild). FK targets
        # hosts.host_id (its uq_hosts_host_id unique column); ON DELETE
        # SET NULL clears the binding when a host is removed, which keeps
        # the workspace-required check satisfied (host_id -> NULL).
        batch_op.create_index("ix_conversations_host_id", ["host_id"])
        batch_op.create_foreign_key(
            "fk_conversations_host_id_hosts",
            "hosts",
            ["host_id"],
            ["host_id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    """Drop the host_id FK + index, then the workspace column and check."""
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_constraint(
            "fk_conversations_host_id_hosts",
            type_="foreignkey",
        )
        batch_op.drop_index("ix_conversations_host_id")
        batch_op.drop_constraint(
            "ck_conversations_workspace_required_for_host",
            type_="check",
        )
        batch_op.drop_column("workspace")
