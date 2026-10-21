"""Add the ``message_reactions`` table.

Revision ID: rx1a2b3c4d5e
Revises: nf12ab34cd56
Create Date: 2026-10-21 00:00:00.000000

One row per (workspace, conversation, response, user): a thumbs-up or
thumbs-down a user gave an assistant response, with an optional note. The PK
leads with ``conversation_id`` so a session's reactions stay contiguous for
the per-session list and the delete cascade, matching ``comments``.

Additive. No existing data needs backfill.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY as MySQLBinary

# BYTEA/BLOB elsewhere, BINARY(16) on MySQL: a BLOB cannot be a primary-key
# column there without a prefix length, and this column leads the PK.
_UUID16 = sa.LargeBinary(length=16).with_variant(MySQLBinary(16), "mysql")

revision: str = "rx1a2b3c4d5e"
down_revision: str | None = "nf12ab34cd56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``message_reactions``.

    Idempotent: skips the DDL when the table already exists so a rerun does
    not crash on ``table already exists``.
    """
    if sa.inspect(op.get_bind()).has_table("message_reactions"):
        return
    op.create_table(
        "message_reactions",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("conversation_id", _UUID16, nullable=False),
        sa.Column("response_id", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("rating", sa.SmallInteger(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("harness", sa.String(length=64), nullable=True),
        sa.Column("model", sa.String(length=256), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id", "response_id", "created_by"),
        sa.CheckConstraint("rating IN (1, 2)", name="ck_message_reactions_rating"),
    )


def downgrade() -> None:
    """Drop ``message_reactions``."""
    op.drop_table("message_reactions")
