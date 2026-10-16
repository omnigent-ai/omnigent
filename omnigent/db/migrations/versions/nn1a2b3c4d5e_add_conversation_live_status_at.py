"""add live_status_at to omnigent_conversation_metadata

Revision ID: nn1a2b3c4d5e
Revises: mm1a2b3c4d5e
Create Date: 2026-10-16 00:00:00.000000

Adds ``live_status_at``: nullable BigInteger, epoch milliseconds at which the
writing replica observed the ``live_status`` it stored. Stamped writes apply
newest-wins, so a replica's delayed write cannot overwrite a newer status
another replica persisted after a session moved. NULL means no stamped write.

Deployment: additive; older application code ignores it and keeps writing
``live_status`` unconditionally. Roll back by downgrading this revision.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "nn1a2b3c4d5e"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(sa.Column("live_status_at", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("live_status_at")
