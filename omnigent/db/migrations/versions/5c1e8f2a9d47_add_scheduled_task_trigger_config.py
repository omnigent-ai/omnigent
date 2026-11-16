"""Add the trigger a scheduled task's prompt is compiled from.

Revision ID: 5c1e8f2a9d47
Revises: f7c2d8a4b901

``trigger_config`` holds what a trigger automation watches, such as Slack
channels, as framed JSON of at most 16,383 bytes (``VARBINARY`` on MySQL).
Tasks are read by primary key, so no index is added.

The column is nullable and nothing reads it yet, so this migration runs online
and every server keeps working on either side of it. Deploy it before the
application release that writes it. That release still saves each trigger's
compiled prompt in ``prompt``, so a server that predates the release keeps
running those tasks from their saved prompts. To roll back, downgrade the
application first. The downgrade then drops the column and the configs in it;
the tasks keep running their saved prompts.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARBINARY

revision: str = "5c1e8f2a9d47"
down_revision: str | None = "f7c2d8a4b901"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Frozen here, so later changes to the model's column type can't alter this migration.
_TRIGGER_CONFIG = sa.LargeBinary().with_variant(VARBINARY(16_383), "mysql")


def upgrade() -> None:
    """Add the nullable trigger."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.add_column(sa.Column("trigger_config", _TRIGGER_CONFIG, nullable=True))


def downgrade() -> None:
    """Drop the trigger."""
    with op.batch_alter_table("scheduled_tasks") as batch:
        batch.drop_column("trigger_config")
