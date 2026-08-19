"""Add git_head_sha to conversation metadata.

Revision ID: 813ae9e6c6d4
Revises: za2b3c4d5e6f
Create Date: 2026-08-12 00:00:00.000000

Records the git HEAD commit SHA at session creation time so the runner can
scope file-change and commit context to this session's lifetime.
"""

import sqlalchemy as sa
from alembic import op

revision: str = "813ae9e6c6d4"
down_revision: str | None = "za2b3c4d5e6f"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(
            sa.Column("git_head_sha", sa.String(length=40), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("git_head_sha")
