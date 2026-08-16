"""add durable turn_operations journal

Revision ID: zb2c3d4e5f6a
Revises: za2b3c4d5e6f
Create Date: 2026-08-16 00:00:00.000000

The journal is the database-enforced replay boundary for a public turn API.
It intentionally records ``dispatch_unknown`` separately so a transport
timeout cannot trigger an unsafe blind redispatch.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY as MySQLBinary

from omnigent.db.db_models import Uuid16

revision: str = "zb2c3d4e5f6a"
down_revision: str | None = "za2b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DIGEST32 = sa.LargeBinary(32).with_variant(MySQLBinary(32), "mysql")


def upgrade() -> None:
    """Create the durable turn-operation journal and replay key."""
    op.create_table(
        "turn_operations",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("principal_hash", _DIGEST32, nullable=False),
        sa.Column("idempotency_key_hash", _DIGEST32, nullable=False),
        sa.Column("request_hash", _DIGEST32, nullable=False),
        sa.Column("request_json", sa.LargeBinary(), nullable=False),
        sa.Column("state", sa.SmallInteger(), nullable=False),
        sa.Column("item_id", Uuid16(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.Column("terminal_at", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error", sa.LargeBinary(), nullable=True),
        sa.CheckConstraint(
            "state IN (1, 2, 3, 4, 5, 6, 7, 8)",
            name="ck_turn_operations_state",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "conversation_id",
            "principal_hash",
            "idempotency_key_hash",
            name="uq_turn_operations_replay_key",
        ),
    )
    op.create_index(
        "ix_turn_operations_conversation_state",
        "turn_operations",
        ["workspace_id", "conversation_id", "state", "created_at", "id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the turn-operation journal."""
    op.drop_index(
        "ix_turn_operations_conversation_state",
        table_name="turn_operations",
    )
    op.drop_table("turn_operations")
