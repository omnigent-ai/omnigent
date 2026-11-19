"""Add project tickets, append-only activity, and an ordinary orchestrator reference.

Revision ID: e8a9b0c1d2e3
Revises: d3bda6dbade3

Deploy this additive schema before the application uses the board. Existing
projects have no orchestrator reference until explicitly initialized. Old code
ignores the new tables and nullable column, but must know this revision to start.
Roll back the application to a schema-aware build to preserve data. A rollback
of this revision destroys ticket data: downgrade only before tickets have been
written, or restore a backup.
Keep the schema change in its own PR before the dependent application PR.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.bounded_binary import BoundedBinary
from omnigent.db.db_models import Uuid16

revision: str = "e8a9b0c1d2e3"
down_revision: str | None = "d3bda6dbade3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    event_constraints = []
    if op.get_bind().dialect.name in ("sqlite", "cloudflare_d1"):
        event_constraints.append(
            sa.CheckConstraint(
                "user_id IS NULL OR length(CAST(user_id AS BLOB)) <= 512",
                name="ck_project_ticket_events_user_id_bytes",
            )
        )
    op.add_column("projects", sa.Column("orchestrator_session_id", Uuid16(), nullable=True))
    op.create_table(
        "project_tickets",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("title", BoundedBinary(1024), nullable=False),
        sa.Column("description", BoundedBinary(16383), nullable=False),
        sa.Column("status", sa.SmallInteger(), nullable=False),
        sa.Column("tags", BoundedBinary(4096), nullable=False),
        sa.Column("completion_summary", BoundedBinary(8192), nullable=True),
        sa.Column("latest_update", BoundedBinary(2048), nullable=False),
        sa.Column("comment_count", sa.Integer(), nullable=False),
        sa.Column("next_event_position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "project_id", "id"),
    )
    op.create_index(
        "ix_project_tickets_created",
        "project_tickets",
        ["workspace_id", "project_id", "created_at", "id"],
    )
    op.create_index(
        "ix_project_tickets_status",
        "project_tickets",
        ["workspace_id", "project_id", "status", "created_at", "id"],
    )
    op.create_table(
        "project_ticket_events",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("ticket_id", Uuid16(), nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("session_id", Uuid16(), nullable=True),
        sa.Column("agent_id", Uuid16(), nullable=True),
        sa.Column("payload", BoundedBinary(16383), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("notification_state", sa.SmallInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "project_id", "ticket_id", "id"),
        *event_constraints,
    )
    op.create_index(
        "ix_project_ticket_events_position",
        "project_ticket_events",
        ["workspace_id", "project_id", "ticket_id", "position", "id"],
    )
    op.create_index(
        "ix_project_ticket_events_kind",
        "project_ticket_events",
        ["workspace_id", "project_id", "ticket_id", "kind", "position", "id"],
    )
    op.create_index(
        "ix_project_ticket_events_notification",
        "project_ticket_events",
        ["notification_state", "workspace_id", "project_id", "ticket_id", "position", "id"],
    )
    op.create_table(
        "project_ticket_sessions",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("ticket_id", Uuid16(), nullable=False),
        sa.Column("session_id", Uuid16(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "project_id", "ticket_id", "session_id"),
    )


def downgrade() -> None:
    op.drop_table("project_ticket_sessions")
    op.drop_table("project_ticket_events")
    op.drop_table("project_tickets")
    op.drop_column("projects", "orchestrator_session_id")
