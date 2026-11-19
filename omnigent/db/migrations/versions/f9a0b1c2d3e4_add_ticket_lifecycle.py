"""Add ticket plans, review briefs, lead sessions and untyped related-ticket links.

Revision ID: f9a0b1c2d3e4
Revises: e8a9b0c1d2e3

Deploy this additive schema before its application changes. Existing status codes
are unchanged; Planning and Review use new codes. Downgrade only before this
feature is used or after backing up its added fields and links.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.bounded_binary import BoundedBinary
from omnigent.db.db_models import Uuid16

revision: str = "f9a0b1c2d3e4"
down_revision: str | None = "e8a9b0c1d2e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("project_tickets", sa.Column("plan", BoundedBinary(16383), nullable=True))
    op.add_column(
        "project_tickets", sa.Column("review_summary", BoundedBinary(8192), nullable=True)
    )
    op.add_column("project_tickets", sa.Column("lead_session_id", Uuid16(), nullable=True))
    op.create_table(
        "project_ticket_links",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("ticket_id", Uuid16(), nullable=False),
        sa.Column("related_ticket_id", Uuid16(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "project_id", "ticket_id", "related_ticket_id"),
    )
    op.create_index(
        "ix_project_ticket_links_reverse",
        "project_ticket_links",
        ["workspace_id", "project_id", "related_ticket_id", "ticket_id"],
    )


def downgrade() -> None:
    op.drop_table("project_ticket_links")
    op.drop_column("project_tickets", "lead_session_id")
    op.drop_column("project_tickets", "review_summary")
    op.drop_column("project_tickets", "plan")
