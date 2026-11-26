"""Add context_breakdown to conversation metadata.

Revision ID: zb1c2d3e4f5a
Revises: bf30d7cbda61
Create Date: 2026-11-21 00:00:00.000000

Adds a nullable ``context_breakdown`` column to
``omnigent_conversation_metadata``. It holds the JSON document of the runner's
context breakdown for the session's last turn, and for a later request that was
rejected as too long for the context window. Stored compressed, like
``pull_requests``.

Additive. No existing data needs backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "zb1c2d3e4f5a"
down_revision: str | None = "bf30d7cbda61"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "omnigent_conversation_metadata"


def upgrade() -> None:
    """Add ``context_breakdown``; skipped when the column already exists."""
    existing = {c["name"] for c in sa.inspect(op.get_bind()).get_columns(_TABLE)}
    if "context_breakdown" not in existing:
        op.add_column(
            _TABLE,
            sa.Column("context_breakdown", sa.LargeBinary(), nullable=True),
        )


def downgrade() -> None:
    """Remove ``context_breakdown``."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column("context_breakdown")
