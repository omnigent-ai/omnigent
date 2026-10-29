"""Add the pending account move column to conversation metadata.

Revision ID: 7a5b2074da28
Revises: d0a1f879a01d
Create Date: 2026-10-29 00:00:00.000000

Adds a nullable ``account_transition`` column (VARCHAR(8192)) to
``omnigent_conversation_metadata``. While a session moves to another account,
the server stores the destination there, so a dropped connection can resume or
cancel the move instead of silently returning to the old route. The store caps
every value at 8192 bytes. The column is NULL for every other session.

Additive. No existing data needs backfill. Downgrade refuses to drop the
column while any move is still pending.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7a5b2074da28"
down_revision: str | None = "d0a1f879a01d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "omnigent_conversation_metadata"
_COLUMN = "account_transition"


def upgrade() -> None:
    """Add ``account_transition`` to ``omnigent_conversation_metadata``."""
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(8192), nullable=True))


def downgrade() -> None:
    """Drop the column unless a move is still pending."""
    pending = (
        op.get_bind()
        .execute(sa.text(f"SELECT EXISTS (SELECT 1 FROM {_TABLE} WHERE {_COLUMN} IS NOT NULL)"))
        .scalar_one()
    )
    if pending:
        raise RuntimeError("Cannot downgrade while account moves are pending")
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column(_COLUMN)
