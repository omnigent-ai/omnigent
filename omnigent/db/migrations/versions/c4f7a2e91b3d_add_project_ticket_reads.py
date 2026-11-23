"""Add per-viewer ticket read markers, seeded so existing activity reads as seen.

Revision ID: c4f7a2e91b3d
Revises: 6a7b8c9d0e1f

Additive: old code ignores the new table. Downgrade drops only the read markers.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "c4f7a2e91b3d"
down_revision: str | None = "6a7b8c9d0e1f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_ticket_reads",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("ticket_id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("seen_position", sa.Integer(), nullable=False),
        sa.Column("seen_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "project_id", "ticket_id", "user_id"),
    )
    # Projects are owner-private, so the owner is the only viewer to seed.
    op.execute(
        sa.text(
            "INSERT INTO project_ticket_reads "
            "(workspace_id, project_id, ticket_id, user_id, seen_position, seen_at) "
            "SELECT t.workspace_id, t.project_id, t.id, COALESCE(p.user_id, 'local'), "
            "t.next_event_position - 1, t.updated_at "
            "FROM project_tickets t JOIN projects p "
            "ON p.workspace_id = t.workspace_id AND p.id = t.project_id"
        )
    )


def downgrade() -> None:
    op.drop_table("project_ticket_reads")
