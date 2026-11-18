"""Add pull_requests to conversation metadata.

Revision ID: d3f7a1c9e2b4
Revises: c8e4f2a6b901

Adds a nullable, compressed ``pull_requests`` column to
``omnigent_conversation_metadata``: the server's copy of the pull requests each
session's host reports, so the Pull Requests tab can list them while the host
is offline.

Additive with no default or backfill. Deploy before the server that writes it;
older servers ignore the column. Downgrade discards the stored copies, which
hosts report again.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d3f7a1c9e2b4"
down_revision: str | None = "c8e4f2a6b901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "omnigent_conversation_metadata"


def upgrade() -> None:
    """Add ``pull_requests`` to ``omnigent_conversation_metadata``."""
    op.add_column(_TABLE, sa.Column("pull_requests", sa.LargeBinary(), nullable=True))


def downgrade() -> None:
    """Remove ``pull_requests`` from ``omnigent_conversation_metadata``."""
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column("pull_requests")
