"""Split conversations table into conversations + omnigent_conversation_metadata.

Revision ID: aa1b2c3d4e5f
Revises: z5a2b3c4d5e6
Create Date: 2026-07-10 00:00:00.000000

Splits Omnigent operational metadata out of the ``conversations`` table into a
new ``omnigent_conversation_metadata`` table (1-to-1 paired by
``(workspace_id, id)``). The columns moved are: ``kind``, ``runner_id``,
``host_id``, ``sub_agent_name``, ``external_session_id``, ``session_state``,
``session_usage``, ``terminal_launch_args``, ``workspace``, ``git_branch``,
``archived``. The ``conversations`` table is left with only AP-side fields.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "aa1b2c3d4e5f"
down_revision: str | None = "z5a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Create the new omnigent_conversation_metadata table.
    op.create_table(
        "omnigent_conversation_metadata",
        sa.Column(
            "workspace_id",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("kind", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("runner_id", sa.String(64), nullable=True),
        sa.Column("host_id", sa.String(64), nullable=True),
        sa.Column("sub_agent_name", sa.String(128), nullable=True),
        sa.Column("external_session_id", sa.String(128), nullable=True),
        sa.Column("session_state", sa.LargeBinary(), nullable=True),
        sa.Column("session_usage", sa.LargeBinary(), nullable=True),
        sa.Column("terminal_launch_args", sa.LargeBinary(), nullable=True),
        sa.Column("workspace", sa.String(2048), nullable=True),
        sa.Column("git_branch", sa.String(255), nullable=True),
        sa.Column(
            "archived",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.CheckConstraint("kind IN (1, 2)", name="ck_conversation_metadata_kind"),
        sa.CheckConstraint(
            "host_id IS NULL OR workspace IS NOT NULL",
            name="ck_conversation_metadata_workspace_required_for_host",
        ),
    )
    op.create_index(
        "ix_conversation_metadata_kind",
        "omnigent_conversation_metadata",
        ["workspace_id", "kind", "id"],
    )
    op.create_index(
        "ix_conversation_metadata_runner_id",
        "omnigent_conversation_metadata",
        ["workspace_id", "runner_id", "id"],
    )

    # 2. Copy data from conversations into the new table.
    op.execute(
        """
        INSERT INTO omnigent_conversation_metadata
            (workspace_id, id, kind, runner_id, host_id, sub_agent_name,
             external_session_id, session_state, session_usage,
             terminal_launch_args, workspace, git_branch, archived)
        SELECT workspace_id, id, kind, runner_id, host_id, sub_agent_name,
               external_session_id, session_state, session_usage,
               terminal_launch_args, workspace, git_branch, archived
        FROM conversations
        """
    )

    # 3. Drop indexes on conversations that covered metadata columns.
    op.drop_index("ix_conversations_kind", table_name="conversations")
    op.drop_index("ix_conversations_runner_id", table_name="conversations")

    # 4. Drop check constraints on conversations that covered metadata columns.
    # Use batch_alter_table for SQLite compatibility.
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_constraint("ck_conversations_kind", type_="check")
        batch_op.drop_constraint("ck_conversations_workspace_required_for_host", type_="check")
        # 5. Drop the metadata columns from conversations.
        batch_op.drop_column("kind")
        batch_op.drop_column("runner_id")
        batch_op.drop_column("host_id")
        batch_op.drop_column("sub_agent_name")
        batch_op.drop_column("external_session_id")
        batch_op.drop_column("session_state")
        batch_op.drop_column("session_usage")
        batch_op.drop_column("terminal_launch_args")
        batch_op.drop_column("workspace")
        batch_op.drop_column("git_branch")
        batch_op.drop_column("archived")


def downgrade() -> None:
    # 1. Re-add the metadata columns to conversations.
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(sa.Column("kind", sa.SmallInteger(), nullable=True))
        batch_op.add_column(sa.Column("runner_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("host_id", sa.String(64), nullable=True))
        batch_op.add_column(sa.Column("sub_agent_name", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("external_session_id", sa.String(128), nullable=True))
        batch_op.add_column(sa.Column("session_state", sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column("session_usage", sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column("terminal_launch_args", sa.LargeBinary(), nullable=True))
        batch_op.add_column(sa.Column("workspace", sa.String(2048), nullable=True))
        batch_op.add_column(sa.Column("git_branch", sa.String(255), nullable=True))
        batch_op.add_column(
            sa.Column(
                "archived",
                sa.Boolean(),
                nullable=True,
                server_default=sa.false(),
            )
        )
        batch_op.create_check_constraint("ck_conversations_kind", "kind IN (1, 2)")
        batch_op.create_check_constraint(
            "ck_conversations_workspace_required_for_host",
            "host_id IS NULL OR workspace IS NOT NULL",
        )

    # 2. Restore data from metadata table back into conversations.
    # Use correlated subqueries to stay compatible with SQLite, MySQL,
    # and PostgreSQL (the UPDATE … FROM form is PostgreSQL-only).
    # ``kind`` is NOT NULL in the pre-split schema; default to 1
    # ("default") for any conversation without a matching metadata row.
    op.execute(
        """
        UPDATE conversations
        SET kind = COALESCE(
            (SELECT m.kind
             FROM omnigent_conversation_metadata m
             WHERE m.workspace_id = conversations.workspace_id
               AND m.id = conversations.id),
            1
        )
        """
    )
    # ``workspace`` must be restored BEFORE ``host_id``: the check constraint
    # re-created above (host_id IS NULL OR workspace IS NOT NULL) is checked
    # per statement, so restoring host_id first would fire it on every
    # host-bound row while its workspace is still NULL.
    for col in (
        "runner_id",
        "workspace",
        "host_id",
        "sub_agent_name",
        "external_session_id",
        "session_state",
        "session_usage",
        "terminal_launch_args",
        "git_branch",
        "archived",
    ):
        op.execute(
            f"""
            UPDATE conversations
            SET {col} = (
                SELECT m.{col}
                FROM omnigent_conversation_metadata m
                WHERE m.workspace_id = conversations.workspace_id
                  AND m.id = conversations.id
            )
            """
        )

    # 3. Re-create indexes that were dropped.
    op.create_index(
        "ix_conversations_kind",
        "conversations",
        ["workspace_id", "kind", "id"],
    )
    op.create_index(
        "ix_conversations_runner_id",
        "conversations",
        ["workspace_id", "runner_id", "id"],
    )

    # 4. Drop the metadata table.
    op.drop_index(
        "ix_conversation_metadata_runner_id", table_name="omnigent_conversation_metadata"
    )
    op.drop_index("ix_conversation_metadata_kind", table_name="omnigent_conversation_metadata")
    op.drop_table("omnigent_conversation_metadata")
