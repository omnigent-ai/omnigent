"""Add append-only project KPI revisions.

Revision ID: c9e5a7b3d1f2
Revises: b8d4f6a2c3e1

Additive: older binaries ignore the new table. Downgrade drops it, discarding KPI history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.bounded_binary import BoundedBinary
from omnigent.db.db_models import Uuid16

revision: str = "c9e5a7b3d1f2"
down_revision: str | None = "b8d4f6a2c3e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_kpi_revisions",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kpis", BoundedBinary(16383), nullable=False),
        sa.Column("created_by_user_id", sa.String(128), nullable=True),
        sa.Column("created_by_session_id", Uuid16(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "project_id", "revision"),
    )


def downgrade() -> None:
    op.drop_table("project_kpi_revisions")
