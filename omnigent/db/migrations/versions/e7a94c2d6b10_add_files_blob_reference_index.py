"""Index workspace blob references for bounded ownership checks.

Revision ID: e7a94c2d6b10
Revises: c6e9a2b7d481
"""

from __future__ import annotations

from alembic import op

revision: str = "e7a94c2d6b10"
down_revision: str = "c6e9a2b7d481"
branch_labels = None
depends_on = None

_INDEX = "ix_files_blob_reference"


def upgrade() -> None:
    op.create_index(_INDEX, "files", ["workspace_id", "blob_key", "id"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="files")
