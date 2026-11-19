"""Add the session a scheduled task's runs start as a copy of.

Revision ID: 1ad1db53f99d
Revises: f1c7a9e2b406

``source_session_id`` names a session each run copies: the run's new session
begins with that session's history. Like ``session_id``, it relates to
``conversations.id`` with no database foreign key. Tasks are read by primary
key, so no index is added. Downgrade drops the column, so those tasks start
each run in an empty session again.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "1ad1db53f99d"
down_revision: str | None = "f1c7a9e2b406"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Add the nullable source session."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.add_column(sa.Column("source_session_id", Uuid16(), nullable=True))


def downgrade() -> None:
    """Drop the source session."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.drop_column("source_session_id")
