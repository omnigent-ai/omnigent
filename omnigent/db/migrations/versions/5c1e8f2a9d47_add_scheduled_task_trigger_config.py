"""Add the trigger a scheduled task's prompt is compiled from.

Revision ID: 5c1e8f2a9d47
Revises: f7c2d8a4b901

``trigger_config`` holds what a trigger automation watches, such as Slack
channels, as a storage envelope (``omnigent.db.envelope``) of at most 16,383
bytes (``VARBINARY`` on MySQL).
Tasks are read by primary key, so no index is added.

The column is nullable, so this migration runs online, and servers already
running from before it keep working: they don't read the column. An older
server that starts after it refuses the database revision it doesn't know.
The model maps the column, so every read of a task selects it, and the
migration must run before any server with that mapping starts. Servers apply
pending migrations when they first open the database; a deployment that runs
migrations separately must apply this one first. The release that writes the
column still saves each trigger's compiled prompt in ``prompt``, so a server
without that release keeps running those tasks from their saved prompts.

For rollback, stop every server that has this migration and keep them from
restarting, downgrade to ``f7c2d8a4b901`` with one of their builds, then start
the older release. The downgrade drops the column and the configs in it; the
tasks keep running their saved prompts.
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
