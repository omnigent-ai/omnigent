"""Add session_browser_tabs.

Revision ID: a3f7412aa11a
Revises: d0a1f879a01d

Adds the ``session_browser_tabs`` table: each user's open embedded-browser
tabs per session, so they reopen on the user's other devices and logins.

A new table with no backfill; older servers ignore it. Downgrade drops the
saved tabs, and clients keep their own local copies.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "a3f7412aa11a"
down_revision: str | None = "d0a1f879a01d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "session_browser_tabs"


def upgrade() -> None:
    """Create the ``session_browser_tabs`` table."""
    op.create_table(
        _TABLE,
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        # Relates to conversations.id; no DB FK (Rule R032).
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("revision", sa.BigInteger(), nullable=False),
        # JSON tab list stored compressed (CompressedText → LargeBinary).
        sa.Column("tabs", sa.LargeBinary(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id", "user_id"),
    )


def downgrade() -> None:
    """Drop the ``session_browser_tabs`` table."""
    op.drop_table(_TABLE)
