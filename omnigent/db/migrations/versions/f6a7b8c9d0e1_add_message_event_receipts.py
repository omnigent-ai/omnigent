"""Add durable message-event idempotency receipts.

Revision ID: f6a7b8c9d0e1
Revises: e5d9bc8ac650
Create Date: 2026-08-29 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY as MySQLBinary
from sqlalchemy.dialects.mysql import VARCHAR as MySQLVarchar

revision: str = "f6a7b8c9d0e1"
down_revision: str | None = "e5d9bc8ac650"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create durable receipts for logical message submissions."""
    uuid_type = sa.LargeBinary(length=16).with_variant(MySQLBinary(16), "mysql")
    digest_type = sa.LargeBinary(length=32).with_variant(MySQLBinary(32), "mysql")
    event_id_type = sa.String(length=128).with_variant(
        MySQLVarchar(128, collation="utf8mb4_bin"),
        "mysql",
    )
    op.create_table(
        "message_event_receipts",
        sa.Column("workspace_id", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("conversation_id", uuid_type, nullable=False),
        sa.Column("client_event_id", event_id_type, nullable=False),
        sa.Column("fingerprint", digest_type, nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("outcome", sa.Text(), nullable=True),
        # Nullable so a rolling rollback to a binary that does not know about
        # ownership can coexist with the upgraded schema.
        sa.Column("owner_id", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'failed', 'uncertain')",
            name="ck_message_event_receipts_status",
        ),
        sa.CheckConstraint(
            "(status = 'completed' AND outcome IS NOT NULL) OR "
            "(status IN ('pending', 'failed', 'uncertain') AND outcome IS NULL)",
            name="ck_message_event_receipts_outcome",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id", "client_event_id"),
    )


def downgrade() -> None:
    """Drop message-event idempotency receipts."""
    op.drop_table("message_event_receipts")
