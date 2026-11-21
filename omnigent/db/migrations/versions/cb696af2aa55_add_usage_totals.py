"""Add spawn-tree and project usage totals.

Revision ID: cb696af2aa55
Revises: 5ab01aaca219

``omnigent_conversation_metadata.subtree_usage`` holds a top-level session's
usage summed over its whole spawn tree, and ``projects.project_usage`` the sum
of a project's top-level sessions. Both use the ``session_usage`` JSON layout,
compressed and capped at 4,096 stored bytes (``VARBINARY`` on MySQL). They are
read whole by primary key, never filtered in SQL, so no index is added.

Additive, nullable and without a default: ``NULL`` marks a row whose total is
not tracked, so no backfill runs. Ship before the server that maintains them;
older servers ignore both columns. Downgrade drops them and the stored totals.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARBINARY

revision: str = "cb696af2aa55"
down_revision: str | None = "5ab01aaca219"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_MAX_STORED_BYTES = 4096


def _usage_totals() -> sa.types.TypeEngine[bytes]:
    return sa.LargeBinary().with_variant(VARBINARY(_MAX_STORED_BYTES), "mysql")


def upgrade() -> None:
    """Add both totals, untracked on every existing row."""
    op.add_column(
        "omnigent_conversation_metadata",
        sa.Column("subtree_usage", _usage_totals(), nullable=True),
    )
    op.add_column("projects", sa.Column("project_usage", _usage_totals(), nullable=True))


def downgrade() -> None:
    """Drop both totals."""
    with op.batch_alter_table("projects") as batch:
        batch.drop_column("project_usage")
    with op.batch_alter_table("omnigent_conversation_metadata") as batch:
        batch.drop_column("subtree_usage")
