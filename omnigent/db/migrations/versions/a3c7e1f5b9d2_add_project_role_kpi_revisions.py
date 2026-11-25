"""Add append-only KPI revisions for project roles.

Revision ID: a3c7e1f5b9d2
Revises: f2b6d9a4c8e1

Adds the ``project_role_kpi_revisions`` table, keyed by project, role and
revision, alongside the project's own ``project_kpi_revisions``. No data
changes.

Rollback: older binaries ignore the table, so project KPIs are untouched and
role KPIs simply don't show. Downgrade drops the table, discarding role KPI
history.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.bounded_binary import BoundedBinary
from omnigent.db.db_models import Uuid16

revision: str = "a3c7e1f5b9d2"
down_revision: str | None = "f2b6d9a4c8e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "project_role_kpi_revisions" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "project_role_kpi_revisions",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("identity_id", Uuid16(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("kpis", BoundedBinary(16383), nullable=False),
        sa.Column("created_by_user_id", sa.String(128), nullable=True),
        sa.Column("created_by_session_id", Uuid16(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "project_id", "identity_id", "revision"),
    )


def downgrade() -> None:
    op.drop_table("project_role_kpi_revisions")
