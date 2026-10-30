"""Add sidebar live-state columns to conversation metadata.

Revision ID: c7cd88f24c1c
Revises: d0a1f879a01d

Adds two nullable columns to ``omnigent_conversation_metadata``, beside the
other live-state columns so their writes cannot bump
``conversations.updated_at``:

- ``background_work_running`` (Boolean): whether the agent's work outlives its
  ended turn, e.g. a background shell, so any replica's session list can show
  the session as working.
- ``blocked_on`` (String(128)): why a running session's agent is parked on a
  terminal prompt, e.g. "permission prompt", so any replica's session list can
  show the session as needing input.

SQLite does not enforce VARCHAR lengths, so there ``blocked_on`` also gets a
CHECK bounding its stored bytes to 512 (128 characters at up to four bytes
each). The column is added with the CHECK in one ``ADD COLUMN``, so the table
is not rebuilt.

Additive with no default or backfill. Deploy before the server that writes
them; older servers ignore both columns. Downgrade drops them, discarding the
stored values, which runners report again on their next change.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c7cd88f24c1c"
down_revision: str | None = "d0a1f879a01d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "omnigent_conversation_metadata"
_BLOCKED_ON_CHECK = "ck_conversation_metadata_blocked_on_bytes"


def upgrade() -> None:
    """Add ``background_work_running`` and ``blocked_on``."""
    op.add_column(_TABLE, sa.Column("background_work_running", sa.Boolean(), nullable=True))
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN blocked_on VARCHAR(128) "
            f"CONSTRAINT {_BLOCKED_ON_CHECK} "
            "CHECK (blocked_on IS NULL OR length(CAST(blocked_on AS BLOB)) <= 512)"
        )
    else:
        op.add_column(_TABLE, sa.Column("blocked_on", sa.String(128), nullable=True))


def downgrade() -> None:
    """Remove ``background_work_running`` and ``blocked_on``."""
    with op.batch_alter_table(_TABLE) as batch_op:
        if op.get_bind().dialect.name == "sqlite":
            # The rebuilt table would otherwise keep a CHECK on the dropped column.
            batch_op.drop_constraint(_BLOCKED_ON_CHECK, type_="check")
        batch_op.drop_column("blocked_on")
        batch_op.drop_column("background_work_running")
