"""Add draft workflows and their planning sessions.

Revision ID: 9a2c7e6b4f10
Revises: 3b9e4c71d2a8

Existing workflows remain runnable. Apply before upgrading the server.
Downgrade removes drafts and session links; saved plans remain intact.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "9a2c7e6b4f10"
down_revision: str | None = "3b9e4c71d2a8"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Keep draft state cheap to read on list pages without loading plans."""
    with op.batch_alter_table("workflows") as batch:
        batch.add_column(
            sa.Column("is_draft", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch.add_column(sa.Column("planning_session_id", Uuid16(), nullable=True))


def downgrade() -> None:
    """Older servers cannot interpret a workflow with an empty plan."""
    workflows = sa.table("workflows", sa.column("is_draft", sa.Boolean()))
    op.execute(workflows.delete().where(workflows.c.is_draft.is_(True)))
    with op.batch_alter_table("workflows") as batch:
        batch.drop_column("planning_session_id")
        batch.drop_column("is_draft")
