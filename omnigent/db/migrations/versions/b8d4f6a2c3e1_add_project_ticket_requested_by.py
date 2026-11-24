"""Record who requested each project ticket.

Revision ID: b8d4f6a2c3e1
Revises: a7c3e5f9b1d2

Additive: old code ignores the column. Existing tickets take the user who created them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b8d4f6a2c3e1"
down_revision: str | None = "a7c3e5f9b1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("project_tickets", sa.Column("requested_by", sa.String(256), nullable=True))
    op.execute(
        sa.text(
            "UPDATE project_tickets SET requested_by = ("
            "SELECT e.user_id FROM project_ticket_events e "
            "WHERE e.workspace_id = project_tickets.workspace_id "
            "AND e.project_id = project_tickets.project_id "
            "AND e.ticket_id = project_tickets.id AND e.position = 0)"
        )
    )


def downgrade() -> None:
    with op.batch_alter_table("project_tickets") as batch:
        batch.drop_column("requested_by")
