"""Add project stage definitions and ticket stage IDs without rewriting existing data.

Revision ID: 1b2c3d4e5f6a
Revises: 0a1b2c3d4e5f

Deploy this schema before the application. NULL definitions mean the five default
stages; a NULL stage_id resolves from the original status code. This avoids an
unbounded backfill and keeps older writers safe during the schema-only rollout.
Upgrade all ticket writers before using the new application: older writers cannot
interpret custom stages. Keep the original status column and index for rollback.
Downgrade only before stage-aware writes; otherwise retain the additive schema
and restore a coordinated backup before rolling the application back.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.bounded_binary import BoundedBinary

revision: str = "1b2c3d4e5f6a"
down_revision: str | None = "0a1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("ticket_stages", BoundedBinary(16383), nullable=True))
    op.add_column("project_tickets", sa.Column("stage_id", sa.String(64), nullable=True))
    op.create_index(
        "ix_project_tickets_stage",
        "project_tickets",
        ["workspace_id", "project_id", "stage_id", "created_at", "id"],
    )
    op.create_index(
        "ix_project_tickets_legacy_stage",
        "project_tickets",
        ["workspace_id", "project_id", "stage_id", "status", "created_at", "id"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    tickets = sa.table("project_tickets", sa.column("stage_id"))
    projects = sa.table("projects", sa.column("ticket_stages"))
    for column in (tickets.c.stage_id, projects.c.ticket_stages):
        if connection.scalar(sa.select(column).where(column.is_not(None)).limit(1)) is not None:
            raise RuntimeError(
                "Stage data exists; retain this schema or restore a pre-upgrade backup"
            )
    op.drop_index("ix_project_tickets_legacy_stage", table_name="project_tickets")
    op.drop_index("ix_project_tickets_stage", table_name="project_tickets")
    op.drop_column("project_tickets", "stage_id")
    op.drop_column("projects", "ticket_stages")
