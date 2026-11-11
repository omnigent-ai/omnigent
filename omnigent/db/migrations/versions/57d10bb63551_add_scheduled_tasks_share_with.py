"""add share_with column to scheduled_tasks

Revision ID: 57d10bb63551
Revises: a7d4c2e9f610
Create Date: 2026-11-11 00:00:00.000000

Adds a nullable ``share_with`` (VARCHAR(2048)) column to ``scheduled_tasks``: a
JSON list of the principals (users or groups) and permission levels every
session a task creates is shared with, e.g.
``[{"user_id": "team.engineering", "level": 1}]``. ``NULL`` means runs stay
private to the owner, which is how existing tasks behave. The column is never
filtered or indexed.

Stored values stay at or below 8,192 bytes (2,048 characters of up to four
bytes each): PostgreSQL, CockroachDB and MySQL enforce the declared length, and
SQLite, which ignores it, gets a CHECK on the encoded byte length.

Additive, with no backfill. Deploy it before the application change that
reads and writes the column; until then nothing uses it. Roll the application
back first and keep the column: a binary that maps it names it in every
insert, so dropping it while such a binary runs breaks task creation.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "57d10bb63551"
down_revision: str | None = "a7d4c2e9f610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scheduled_tasks"
_BYTES_CHECK = "ck_scheduled_tasks_share_with_bytes"
# Dialects that store a value of any length in any column.
_SQLITE_FAMILY = frozenset({"sqlite", "cloudflare_d1"})


def upgrade() -> None:
    """Add share_with to scheduled_tasks."""
    if op.get_bind().dialect.name in _SQLITE_FAMILY:
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN share_with VARCHAR(2048) "
            f"CONSTRAINT {_BYTES_CHECK} "
            "CHECK (share_with IS NULL OR length(CAST(share_with AS BLOB)) <= 8192)"
        )
    else:
        op.add_column(_TABLE, sa.Column("share_with", sa.String(length=2048), nullable=True))


def downgrade() -> None:
    """Remove share_with from scheduled_tasks."""
    with op.batch_alter_table(_TABLE) as batch_op:
        if op.get_bind().dialect.name in _SQLITE_FAMILY:
            # The rebuilt table would otherwise keep a CHECK on the dropped column.
            batch_op.drop_constraint(_BYTES_CHECK, type_="check")
        batch_op.drop_column("share_with")
