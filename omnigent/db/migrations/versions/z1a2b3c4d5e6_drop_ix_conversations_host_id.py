"""Drop the ``ix_conversations_host_id`` index.

Revision ID: z1a2b3c4d5e6
Revises: y1a2b3c4d5e6
Create Date: 2026-07-08 01:00:00.000000

The index existed solely to serve the ``list_conversations_by_host_id``
lookup, which had no callers and has been removed alongside this
migration. ``conversations.host_id`` carries no FK (removed in
``p1a2b3c4d5e6``), so nothing else depends on it being indexed, and the
index only added write overhead.

Dropping an index is a simple operation on SQLite, PostgreSQL, and
MySQL alike, so no table rebuild / batch mode is needed.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "z1a2b3c4d5e6"
down_revision: str | None = "y1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Drop the now-unused ``ix_conversations_host_id`` index."""
    op.drop_index("ix_conversations_host_id", table_name="conversations")


def downgrade() -> None:
    """Recreate the ``ix_conversations_host_id`` index."""
    op.create_index("ix_conversations_host_id", "conversations", ["host_id"])
