"""Add derived per-project merged-PR rows for the project Summary.

Revision ID: c5e1a7d3f9b2
Revises: d2f6a8c4e1b9

Adds ``project_merged_prs`` (one row per counted merge, indexed by merge time)
and ``project_merged_pr_refreshes`` (when each project's rows were last
computed). Both start empty: the server fills a project's rows on its first
Summary read and keeps them current from its writes, so no data is migrated
here and ticket payloads are only decoded by the running server.

Rollback: older binaries ignore both tables and compute "PRs merged" from
sessions and tickets as before. Downgrade drops both; the rows are derived.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "c5e1a7d3f9b2"
down_revision: str | None = "d2f6a8c4e1b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_merged_prs",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("pr_key", sa.String(512), nullable=False),
        sa.Column("merged_at", sa.BigInteger(), nullable=True),
        sa.Column("member_key", sa.String(256), nullable=True),
        sa.Column("claimed_by", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "project_id", "pr_key"),
    )
    op.create_index(
        "ix_project_merged_prs_merged_at",
        "project_merged_prs",
        ["workspace_id", "project_id", "merged_at"],
    )
    op.create_table(
        "project_merged_pr_refreshes",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("project_id", Uuid16(), nullable=False),
        sa.Column("refreshed_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "project_id"),
    )


def downgrade() -> None:
    op.drop_table("project_merged_pr_refreshes")
    op.drop_index("ix_project_merged_prs_merged_at", table_name="project_merged_prs")
    op.drop_table("project_merged_prs")
