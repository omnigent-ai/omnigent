"""bind turn operations to a runner incarnation

Revision ID: zc3d4e5f6a7b
Revises: zb2c3d4e5f6a
Create Date: 2026-08-16 00:00:00.000000

The binding prevents a coordinator from treating a missing operation after a
runner restart as proof that the original dispatch never executed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY as MySQLBinary

revision: str = "zc3d4e5f6a7b"
down_revision: str | None = "zb2c3d4e5f6a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIGEST32 = sa.LargeBinary(32).with_variant(MySQLBinary(32), "mysql")


def upgrade() -> None:
    """Add the runner binding and dispatch-attempt audit fields."""
    op.add_column(
        "turn_operations",
        sa.Column("dispatch_request_hash", _DIGEST32, nullable=True),
    )
    op.add_column(
        "turn_operations",
        sa.Column("dispatch_request_json", sa.LargeBinary(), nullable=True),
    )
    op.add_column(
        "turn_operations",
        sa.Column("runner_incarnation_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "turn_operations",
        sa.Column("dispatch_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "turn_operations",
        sa.Column("last_dispatch_at", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Remove runner-binding metadata without altering journal entries."""
    with op.batch_alter_table("turn_operations") as batch_op:
        batch_op.drop_column("last_dispatch_at")
        batch_op.drop_column("dispatch_attempts")
        batch_op.drop_column("runner_incarnation_id")
        batch_op.drop_column("dispatch_request_json")
        batch_op.drop_column("dispatch_request_hash")
