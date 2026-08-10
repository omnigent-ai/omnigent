"""Add display_name to conversation metadata.

Revision ID: za2b3c4d5e6f
Revises: z9a2b3c4d5e6
Create Date: 2026-08-10 00:00:00.000000

Adds a nullable ``display_name`` column to ``omnigent_conversation_metadata``.
Sub-agent sessions use this column to store a human-readable, task-derived label
(e.g. "Investigate auth token refresh") generated asynchronously by the
background title coordinator. The structured title
(``"{agent_type}:{agent_type}-{ordinal}"``) stays in ``conversations.title`` as
the stable spawn-or-continue key; ``display_name`` is purely presentational.

Additive. No existing data needs backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "za2b3c4d5e6f"
down_revision: str | None = "z9a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add ``display_name`` to ``omnigent_conversation_metadata``."""
    op.add_column(
        "omnigent_conversation_metadata",
        sa.Column("display_name", sa.String(128), nullable=True),
    )


def downgrade() -> None:
    """Remove ``display_name`` from ``omnigent_conversation_metadata``."""
    op.drop_column("omnigent_conversation_metadata", "display_name")
