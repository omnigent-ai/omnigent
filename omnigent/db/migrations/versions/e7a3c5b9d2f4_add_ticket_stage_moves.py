"""Add ticket stage moves for project status metrics.

Revision ID: e7a3c5b9d2f4
Revises: c5e1a7d3f9b2

Additive: older binaries ignore both tables. The server builds each project's
rows from its ticket events on first read, through the store, so encrypted event
payloads are read with the deployment's cipher. Downgrade drops both tables.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "e7a3c5b9d2f4"
down_revision: str | None = "c5e1a7d3f9b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_ticket_stage_moves",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("ticket_id", Uuid16(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("moved_at", sa.BigInteger(), nullable=False),
        sa.Column("stage_id", sa.String(64), nullable=False),
        sa.Column("is_complete", sa.Boolean(), nullable=False),
        sa.Column("is_creation", sa.Boolean(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("session_id", Uuid16(), nullable=True),
        sa.Column("member_key", sa.String(160), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "project_id", "ticket_id", "position"),
    )
    op.create_index(
        "ix_project_ticket_stage_moves_time",
        "project_ticket_stage_moves",
        ["workspace_id", "project_id", "moved_at"],
    )
    op.create_table(
        "project_ticket_stage_move_builds",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("built_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "project_id"),
    )


def downgrade() -> None:
    op.drop_table("project_ticket_stage_move_builds")
    op.drop_table("project_ticket_stage_moves")
