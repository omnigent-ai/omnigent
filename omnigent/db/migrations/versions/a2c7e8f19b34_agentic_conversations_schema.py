"""agentic sessions: agent binding column on conversations

Revision ID: a2c7e8f19b34
Revises: c9d3a1f2e4b5
Create Date: 2026-04-21 00:00:00.000000

Adds the agent binding required by the Sessions API. Note that the
underlying table name remains ``conversations`` — that table predates
this PR and is not being renamed in this migration.

- ``conversations``: adds ``agent_id`` (FK to ``agents.id``) so a
  session can be bound to a single agent at create-time.

Session ``status`` and ``active_task_id`` are derived on read from the
``tasks`` table rather than persisted on ``conversations``; this
migration deliberately does NOT add those columns.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a2c7e8f19b34"
down_revision: str | None = "c9d3a1f2e4b5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ── conversations: new agent_id column + FK ───────────
    # SQLite does not support ALTER TABLE ADD CONSTRAINT or
    # ALTER TABLE ADD COLUMN ... REFERENCES, so the column add
    # plus FK goes through batch mode (copy-and-move strategy).
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(
            sa.Column("agent_id", sa.String(length=64), nullable=True),
        )
        batch_op.create_foreign_key(
            "fk_conversations_agent_id",
            "agents",
            ["agent_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    # ── conversations ─────────────────────────────────────
    with op.batch_alter_table("conversations") as batch_op:
        if any(
            f["name"] == "fk_conversations_agent_id"
            for f in sa.inspect(op.get_bind()).get_foreign_keys("conversations")
        ):
            batch_op.drop_constraint("fk_conversations_agent_id", type_="foreignkey")
        batch_op.drop_column("agent_id")
