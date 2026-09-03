"""Add durable versioned DPIA case storage.

Revision ID: dc1a2b3c4d5e
Revises: ga1b2c3d4e5f
Create Date: 2026-08-29 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "dc1a2b3c4d5e"
down_revision: str | None = "ga1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dpia_cases",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("case_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot_json", sa.LargeBinary(), nullable=False),
        sa.Column("created_by", sa.String(128), nullable=False),
        sa.Column("updated_by", sa.String(128), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_dpia_cases_revision"),
        sa.PrimaryKeyConstraint("workspace_id", "case_id"),
    )
    op.create_index(
        "ix_dpia_cases_updated",
        "dpia_cases",
        ["workspace_id", "updated_at", "case_id"],
    )
    op.create_table(
        "dpia_case_revisions",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("case_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot_json", sa.LargeBinary(), nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.CheckConstraint("revision > 0", name="ck_dpia_case_revisions_revision"),
        sa.PrimaryKeyConstraint("workspace_id", "case_id", "revision"),
    )
    op.create_index(
        "ix_dpia_case_revisions_case",
        "dpia_case_revisions",
        ["workspace_id", "case_id", "revision"],
    )


def downgrade() -> None:
    op.drop_index("ix_dpia_case_revisions_case", table_name="dpia_case_revisions")
    op.drop_table("dpia_case_revisions")
    op.drop_index("ix_dpia_cases_updated", table_name="dpia_cases")
    op.drop_table("dpia_cases")
