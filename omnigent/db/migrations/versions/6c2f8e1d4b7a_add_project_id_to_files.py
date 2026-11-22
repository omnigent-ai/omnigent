"""Add project-level files.

Revision ID: 6c2f8e1d4b7a
Revises: 5ab01aaca219

``files.project_id`` records the project a file belongs to. A project file has
``project_id`` set and ``session_id`` NULL; every session filed in the project
can read and write it. ``ix_files_project_id_created_at`` serves listing a
project's files, as ``ix_files_session_id_created_at`` does for a session's.

``ix_files_blob_key`` serves the blob reference check made before deleting
bytes. That check now matches ``blob_key`` directly (falling back to ``id``
only for rows written before ``blob_key`` existed), so with this index it is a
lookup instead of a scan of the workspace's files.

Additive and unused by earlier releases, so this schema can ship before the
application code that reads it. Downgrade drops both indexes and the column,
and with it every project file's membership.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "6c2f8e1d4b7a"
down_revision: str | None = "5ab01aaca219"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

_TABLE = "files"
_PROJECT_INDEX = "ix_files_project_id_created_at"
_BLOB_KEY_INDEX = "ix_files_blob_key"


def upgrade() -> None:
    """Add the project column and the project listing and blob reference indexes."""
    with op.batch_alter_table(_TABLE) as batch:
        batch.add_column(sa.Column("project_id", Uuid16(), nullable=True))
    op.create_index(_PROJECT_INDEX, _TABLE, ["workspace_id", "project_id", "created_at", "id"])
    op.create_index(_BLOB_KEY_INDEX, _TABLE, ["workspace_id", "blob_key", "id"])


def downgrade() -> None:
    """Drop the indexes and the project column."""
    op.drop_index(_BLOB_KEY_INDEX, table_name=_TABLE)
    op.drop_index(_PROJECT_INDEX, table_name=_TABLE)
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_column("project_id")
