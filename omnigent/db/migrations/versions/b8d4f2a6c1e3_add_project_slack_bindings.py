"""Add project Slack channel links, ``project_slack_bindings`` and ``project_slack_audit``.

Revision ID: b8d4f2a6c1e3
Revises: a7c3e9f1b5d2

Additive: two new tables that older binaries ignore. A binding is unique on
its Slack channel and on its project, so the database arbitrates concurrent
claims. Downgrade drops both tables, which unlinks every channel.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16
from omnigent.db.envelope import EnvelopedJSON

revision: str = "b8d4f2a6c1e3"
down_revision: str | None = "a7c3e9f1b5d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    tables = set(sa.inspect(op.get_bind()).get_table_names())
    if "project_slack_bindings" not in tables:
        op.create_table(
            "project_slack_bindings",
            sa.Column("workspace_id", sa.BigInteger(), nullable=False),
            sa.Column("id", Uuid16(), nullable=False),
            sa.Column("project_id", Uuid16(), nullable=False),
            sa.Column("slack_team_id", sa.String(32), nullable=False),
            sa.Column("channel_id", sa.String(32), nullable=False),
            sa.Column("channel_name", sa.String(128), nullable=True),
            sa.Column("state", sa.String(24), nullable=False),
            sa.Column("destination", EnvelopedJSON(4096), nullable=True),
            sa.Column("automation_id", sa.String(128), nullable=True),
            sa.Column("relay_key_hash", sa.String(64), nullable=True),
            sa.Column("last_error", sa.String(1024), nullable=True),
            sa.Column("owner_user_id", sa.String(128), nullable=False),
            sa.Column("created_by_user_id", sa.String(128), nullable=False),
            sa.Column("updated_by_user_id", sa.String(128), nullable=False),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.Column("updated_at", sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint("workspace_id", "id"),
            sa.UniqueConstraint(
                "workspace_id",
                "slack_team_id",
                "channel_id",
                name="uq_project_slack_bindings_channel",
            ),
            sa.UniqueConstraint(
                "workspace_id", "project_id", name="uq_project_slack_bindings_project"
            ),
        )
    if "project_slack_audit" not in tables:
        op.create_table(
            "project_slack_audit",
            sa.Column("workspace_id", sa.BigInteger(), nullable=False),
            sa.Column("id", Uuid16(), nullable=False),
            sa.Column("project_id", Uuid16(), nullable=False),
            sa.Column("binding_id", Uuid16(), nullable=True),
            sa.Column("actor_user_id", sa.String(128), nullable=True),
            sa.Column("action", sa.String(48), nullable=False),
            sa.Column("detail", EnvelopedJSON(4096), nullable=True),
            sa.Column("created_at", sa.BigInteger(), nullable=False),
            sa.PrimaryKeyConstraint("workspace_id", "id"),
        )
        op.create_index(
            "ix_project_slack_audit_project",
            "project_slack_audit",
            ["workspace_id", "project_id", "created_at"],
        )


def downgrade() -> None:
    op.drop_index("ix_project_slack_audit_project", table_name="project_slack_audit")
    op.drop_table("project_slack_audit")
    op.drop_table("project_slack_bindings")
