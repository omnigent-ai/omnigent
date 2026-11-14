"""Add workflow planning sessions.

Revision ID: 9a2c7e6b4f10
Revises: 3b9e4c71d2a8

Existing workflows remain runnable. Apply before upgrading the server.
Downgrade removes drafts and session links; saved plans remain intact.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

from omnigent.db.compression import CompressedText
from omnigent.db.db_models import Uuid16

revision: str = "9a2c7e6b4f10"
down_revision: str | None = "3b9e4c71d2a8"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Link a workflow to its planning conversation."""
    with op.batch_alter_table("workflows") as batch:
        batch.add_column(sa.Column("planning_session_id", Uuid16(), nullable=True))


def downgrade() -> None:
    """Older servers cannot interpret a workflow with an empty plan."""
    workflows = sa.table(
        "workflows",
        sa.column("workspace_id", sa.BigInteger()),
        sa.column("id", Uuid16()),
        sa.column("plan", CompressedText()),
    )
    connection = op.get_bind()
    for row in connection.execute(sa.select(workflows)).all():
        if not json.loads(row.plan)["steps"]:
            connection.execute(
                workflows.delete().where(
                    workflows.c.workspace_id == row.workspace_id,
                    workflows.c.id == row.id,
                )
            )
    with op.batch_alter_table("workflows") as batch:
        batch.drop_column("planning_session_id")
