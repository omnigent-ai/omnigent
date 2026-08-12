"""Add a visibility/use purpose to stored files.

Revision ID: a8c4e1f6b2d9
Revises: d5e9f1a2b3c4
Create Date: 2026-08-07

Existing files become normal ``user_upload`` records. Generated Computer Use
frames use ``computer_use_frame`` and are filtered out of ordinary session file
lists while retaining the same session ownership and cleanup behavior.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8c4e1f6b2d9"
down_revision: str | None = "d5e9f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_files_session_id_created_at"


def upgrade() -> None:
    """Backfill normal purpose and make purpose part of session list seeks."""
    with op.batch_alter_table("files") as batch_op:
        batch_op.add_column(
            sa.Column(
                "purpose",
                sa.String(length=32),
                nullable=False,
                server_default="user_upload",
            )
        )
        batch_op.drop_index(_INDEX)
        batch_op.create_index(
            _INDEX,
            ["workspace_id", "session_id", "purpose", "created_at", "id"],
            unique=False,
        )


def downgrade() -> None:
    """Restore the pre-purpose file schema and session list index."""
    with op.batch_alter_table("files") as batch_op:
        batch_op.drop_index(_INDEX)
        batch_op.create_index(
            _INDEX,
            ["workspace_id", "session_id", "created_at", "id"],
            unique=False,
        )
        batch_op.drop_column("purpose")
