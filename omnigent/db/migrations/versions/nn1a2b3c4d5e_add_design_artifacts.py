"""add design_artifacts table

Revision ID: nn1a2b3c4d5e
Revises: mm1a2b3c4d5e
Create Date: 2026-10-05 00:00:00.000000

Adds ``design_artifacts``, the Design page's index of slide decks and
wireframes across sessions. See ``designs/DESIGN_PAGE.md`` (Server deck index).

Additive, new table only. There is no foreign key to ``conversations``
(schema Rule R032); rows are scoped by ``(workspace_id, session_id)``.
Roll back by downgrading this revision.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "nn1a2b3c4d5e"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``design_artifacts`` table."""
    op.create_table(
        "design_artifacts",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("session_id", Uuid16(), nullable=False),
        sa.Column("path", sa.String(512), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("deleted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.PrimaryKeyConstraint("workspace_id", "session_id", "path"),
    )
    op.create_index(
        "ix_design_artifacts_session",
        "design_artifacts",
        ["workspace_id", "session_id"],
    )


def downgrade() -> None:
    """Drop the ``design_artifacts`` table."""
    op.drop_index("ix_design_artifacts_session", table_name="design_artifacts")
    op.drop_table("design_artifacts")
