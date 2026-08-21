"""add elicitations table

Revision ID: zb2b3c4d5e6f
Revises: za2b3c4d5e6f
Create Date: 2026-08-21 00:00:00.000000

Adds the ``elicitations`` table: one row per approval prompt still waiting on a
human, so an outstanding prompt survives a server restart instead of dying with
the in-process index in ``omnigent/runtime/pending_elicitations.py``.

Resolving a prompt deletes its row, so the table holds only the parked set and
never accumulates history. The row is the durable mirror of an index that is
otherwise process-local; the count mirrored onto
``omnigent_conversation_metadata.pending_elicitation_count`` already assumed
such a mirror existed, but carried no payload to replay.

The table is brand-new and created at the current schema state, so it carries
the tenant-partition ``workspace_id`` column as the leading primary-key member
(matching every other table after ``r1a2b3c4d5e6``). There is no foreign-key
constraint on ``conversation_id`` (schema Rule R032 — see ``p1a2b3c4d5e6``):
the application deletes these rows alongside the conversation.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "zb2b3c4d5e6f"
down_revision: str | None = "za2b3c4d5e6f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``elicitations`` table."""
    op.create_table(
        "elicitations",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        # The prompt's correlation id. A plain String, not Uuid16: native
        # harnesses mint deterministic ids from the session plus the gated tool
        # call (e.g. "elicit_cursor_<session>_<digest>"), so it is opaque text.
        sa.Column("id", sa.String(128), nullable=False),
        # Relates to conversations.id (no DB FK, Rule R032).
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        # The response.elicitation_request payload as JSON, stored verbatim so a
        # restored prompt replays identically. Opaque, never SQL-queried —
        # stored compressed (CompressedText → LargeBinary).
        sa.Column("event", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_elicitations_conversation_id",
        "elicitations",
        ["workspace_id", "conversation_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the ``elicitations`` table."""
    op.drop_index("ix_elicitations_conversation_id", table_name="elicitations")
    op.drop_table("elicitations")
