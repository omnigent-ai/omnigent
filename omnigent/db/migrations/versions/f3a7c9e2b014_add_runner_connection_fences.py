"""Retain database-owned runner connection fences and heartbeat evidence.

Apply before fenced application readers and writers. Legacy conversation
stamps are not copied: they contain no connection identity. Roll back the
application before removing this table; rows are intentionally tombstones.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision = "f3a7c9e2b014"
down_revision = ("e7c9a2d4f601", "d0a1f879a01d")
branch_labels = None
depends_on = None

_TABLE = "runner_connections"


def upgrade() -> None:
    """Create the runner-level authority without backfilling unfenced stamps."""
    if sa.inspect(op.get_bind()).has_table(_TABLE):
        return
    op.create_table(
        _TABLE,
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("runner_id", sa.String(64), nullable=False),
        sa.Column("epoch", Uuid16(), nullable=False),
        sa.Column("state", sa.SmallInteger(), nullable=False),
        sa.Column("last_seen", sa.BigInteger(), nullable=True),
        sa.Column("last_connected", sa.BigInteger(), nullable=True),
        sa.Column("heartbeat_epoch", Uuid16(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "runner_id"),
        sa.CheckConstraint("state IN (0, 1, 2)", name="ck_runner_connections_state"),
    )


def downgrade() -> None:
    """Remove connection authority only after application rollback."""
    if sa.inspect(op.get_bind()).has_table(_TABLE):
        op.drop_table(_TABLE)
