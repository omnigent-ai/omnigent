"""Add the trigger a scheduled task's prompt was compiled from.

Revision ID: 5c1e8f2a9d47
Revises: f7c2d8a4b901

``trigger_config`` holds what a trigger automation watches, such as a Slack
channel, as JSON. The server compiles it into the task's prompt and polling
rule, so a server that predates the column still runs the saved prompt. Tasks
are read by primary key, so no index is added. Downgrade drops the column; the
tasks keep running their saved prompts.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.compression import CompressedText

revision: str = "5c1e8f2a9d47"
down_revision: str | None = "f7c2d8a4b901"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Add the nullable trigger."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.add_column(sa.Column("trigger_config", CompressedText(), nullable=True))


def downgrade() -> None:
    """Drop the trigger."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.drop_column("trigger_config")
