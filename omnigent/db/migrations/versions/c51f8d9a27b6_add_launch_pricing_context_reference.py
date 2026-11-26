"""Add an optional reference to a session's launch pricing context.

Revision ID: c51f8d9a27b6
Revises: 47d3e8fc3e25

Only a fixed-size artifact identity is stored on conversation metadata. The
application follow-up will retain the complete provider name in artifact
storage, without limiting existing provider names or expanding session_overrides.

This migration adds no application reader, writer, default, index or backfill.
Running older servers can continue using their known columns, but an older
build cannot restart against an unknown Alembic revision. Deploy a build that
knows this migration before applying it; see docs/launch-pricing-storage.md.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "c51f8d9a27b6"
down_revision: str | None = "47d3e8fc3e25"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Add the nullable, fixed-size pricing artifact identity."""
    op.add_column(
        "omnigent_conversation_metadata",
        sa.Column("launch_pricing_context_id", Uuid16(), nullable=True),
    )


def downgrade() -> None:
    """Remove the unused reference before application activation."""
    with op.batch_alter_table("omnigent_conversation_metadata") as batch:
        batch.drop_column("launch_pricing_context_id")
