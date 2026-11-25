"""Add usage events for project telemetry.

Revision ID: d2f6a8c4e1b9
Revises: a3c7e1f5b9d2

Additive: older binaries ignore the new table. Downgrade drops it and the
recorded events, which are pruned after a retention period anyway.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.bounded_binary import BoundedBinary
from omnigent.db.db_models import Uuid16

revision: str = "d2f6a8c4e1b9"
down_revision: str | None = "a3c7e1f5b9d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "usage_events",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=True),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("session_id", Uuid16(), nullable=True),
        sa.Column("surface", sa.String(32), nullable=True),
        sa.Column("subject_id", sa.String(64), nullable=True),
        sa.Column("props", BoundedBinary(2048), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_usage_events_project_time",
        "usage_events",
        ["workspace_id", "project_id", "created_at"],
    )
    op.create_index(
        "ix_usage_events_user_time", "usage_events", ["workspace_id", "user_id", "created_at"]
    )
    op.create_index("ix_usage_events_time", "usage_events", ["workspace_id", "created_at"])


def downgrade() -> None:
    op.drop_table("usage_events")
