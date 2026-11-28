"""Add per-commit line counts for the project Summary's Changes landed.

Revision ID: e3b7d1f5a9c2
Revises: b8d4f2a6c1e3

Adds ``project_commit_line_stats``: one row per (project, landed SHA) with the lines
that commit added and removed, generated files left out, plus the raw totals. Rows
are computed by git on a host and posted by an agent, since the server may not reach
a checkout; the table starts empty and nothing is migrated.

Rollback: older binaries ignore the table and show Changes landed without line
counts. Downgrade drops it; the rows can be recomputed from git.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "e3b7d1f5a9c2"
down_revision: str | None = "b8d4f2a6c1e3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_commit_line_stats",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("sha", sa.String(40), nullable=False),
        sa.Column("added", sa.Integer(), nullable=False),
        sa.Column("removed", sa.Integer(), nullable=False),
        sa.Column("raw_added", sa.Integer(), nullable=False),
        sa.Column("raw_removed", sa.Integer(), nullable=False),
        sa.Column("files", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "project_id", "sha"),
    )


def downgrade() -> None:
    op.drop_table("project_commit_line_stats")
