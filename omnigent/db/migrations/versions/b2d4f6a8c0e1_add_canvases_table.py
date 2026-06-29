"""add canvases table

Revision ID: b2d4f6a8c0e1
Revises: a7c3e9b15d24
Create Date: 2026-06-28 01:00:00.000000

Adds the ``canvases`` table: one agent-authored artifact per conversation
(set via the ``set_canvas`` tool, rendered in the web UI's right-rail Canvas
tab). UNIQUE on ``conversation_id`` — the tool upserts.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2d4f6a8c0e1"
down_revision: str | None = "a7c3e9b15d24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "canvases",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column(
            "content_type",
            sa.String(length=16),
            server_default=sa.text("'html'"),
            nullable=False,
        ),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", name="uq_canvases_conversation_id"),
    )


def downgrade() -> None:
    op.drop_table("canvases")
