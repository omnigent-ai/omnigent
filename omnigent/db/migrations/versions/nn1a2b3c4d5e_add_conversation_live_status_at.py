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


_TABLE = "omnigent_conversation_metadata"
_COLUMN = "live_status_at"


def _column_exists() -> bool:
    return _COLUMN in {column["name"] for column in sa.inspect(op.get_bind()).get_columns(_TABLE)}


def upgrade() -> None:
    # A retry that replays this revision after the column was added must not fail.
    if _column_exists():
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.add_column(sa.Column(_COLUMN, sa.BigInteger(), nullable=True))


def downgrade() -> None:
    if not _column_exists():
        return
    with op.batch_alter_table(_TABLE) as batch_op:
        batch_op.drop_column(_COLUMN)
