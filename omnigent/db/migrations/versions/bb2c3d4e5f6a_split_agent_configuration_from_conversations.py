"""Split agent binding and per-session overrides into agent_configuration.

Revision ID: bb2c3d4e5f6a
Revises: aa1b2c3d4e5f
Create Date: 2026-07-12 00:00:00.000000

Moves the agent binding and per-session config overrides out of the
``conversations`` table into a new ``agent_configuration`` table (1-to-1
paired by ``(workspace_id, conversation_id)``, same database). The
columns moved are: ``agent_id``, ``reasoning_effort``,
``model_override``, ``cost_control_mode_override``,
``harness_override``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "bb2c3d4e5f6a"
down_revision: str | None = "aa1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MOVED_COLUMNS = (
    "agent_id",
    "reasoning_effort",
    "model_override",
    "cost_control_mode_override",
    "harness_override",
)


def upgrade() -> None:
    # 1. Create the new agent_configuration table.
    op.create_table(
        "agent_configuration",
        sa.Column(
            "workspace_id",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("conversation_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=True),
        sa.Column("reasoning_effort", sa.String(32), nullable=True),
        sa.Column("model_override", sa.String(128), nullable=True),
        sa.Column("cost_control_mode_override", sa.String(8), nullable=True),
        sa.Column("harness_override", sa.String(64), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id"),
    )
    op.create_index(
        "ix_agent_configuration_agent_id",
        "agent_configuration",
        ["workspace_id", "agent_id", "conversation_id"],
    )

    # 2. Copy data: one agent_configuration row per conversation.
    op.execute(
        """
        INSERT INTO agent_configuration
            (workspace_id, conversation_id, agent_id, reasoning_effort,
             model_override, cost_control_mode_override, harness_override)
        SELECT workspace_id, id, agent_id, reasoning_effort, model_override,
               cost_control_mode_override, harness_override
        FROM conversations
        """
    )

    # 3. Drop the moved index and columns from conversations.
    # Use batch_alter_table for SQLite compatibility.
    op.drop_index("ix_conversations_agent_id", table_name="conversations")
    with op.batch_alter_table("conversations") as batch_op:
        for col in _MOVED_COLUMNS:
            batch_op.drop_column(col)


def downgrade() -> None:
    # 1. Re-add the moved columns to conversations.
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("agent_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("reasoning_effort", sa.String(32), nullable=True))
        batch_op.add_column(sa.Column("model_override", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("cost_control_mode_override", sa.String(8), nullable=True))
        batch_op.add_column(sa.Column("harness_override", sa.String(64), nullable=True))

    # 2. Restore data via correlated subqueries (portable across SQLite,
    # MySQL, and PostgreSQL; UPDATE ... FROM is PostgreSQL-only).
    for col in _MOVED_COLUMNS:
        op.execute(
            f"""
            UPDATE conversations
            SET {col} = (
                SELECT ac.{col}
                FROM agent_configuration ac
                WHERE ac.workspace_id = conversations.workspace_id
                  AND ac.conversation_id = conversations.id
            )
            """
        )

    # 3. Re-create the dropped index, then drop the new table.
    op.create_index(
        "ix_conversations_agent_id",
        "conversations",
        ["workspace_id", "agent_id", "id"],
    )
    op.drop_index("ix_agent_configuration_agent_id", table_name="agent_configuration")
    op.drop_table("agent_configuration")
