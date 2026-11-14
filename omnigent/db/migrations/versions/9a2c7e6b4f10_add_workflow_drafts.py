"""Add workflow planning sessions.

Revision ID: 9a2c7e6b4f10
Revises: 3b9e4c71d2a8

Existing workflows remain runnable. Apply before upgrading the server.
Downgrade removes drafts and session links; saved plans remain intact. Stop
writers first. Draft cleanup commits bounded batches and can be restarted.
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

_CLEANUP_BATCH = 250


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
    key = sa.tuple_(workflows.c.workspace_id, workflows.c.id)
    query = sa.select(workflows).order_by(workflows.c.workspace_id, workflows.c.id)
    page = connection.execute(query.limit(_CLEANUP_BATCH)).all()
    if page:
        # Each cleanup statement commits independently; the revision is advanced
        # only after cleanup and DDL finish, so an interrupted downgrade is retryable.
        with op.get_context().autocommit_block():
            while page:
                drafts = [
                    (row.workspace_id, row.id) for row in page if not json.loads(row.plan)["steps"]
                ]
                if drafts:
                    connection.execute(workflows.delete().where(key.in_(drafts)))
                after = (page[-1].workspace_id, page[-1].id)
                page = connection.execute(query.where(key > after).limit(_CLEANUP_BATCH)).all()
    with op.batch_alter_table("workflows") as batch:
        batch.drop_column("planning_session_id")
