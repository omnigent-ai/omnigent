"""Add sidebar live-state columns to conversation metadata.

Revision ID: c7cd88f24c1c
Revises: d0a1f879a01d

Adds three nullable columns to ``omnigent_conversation_metadata``, beside the
other live-state columns so their writes cannot bump
``conversations.updated_at``:

- ``background_work_running`` (Boolean): whether the agent's work outlives its
  ended turn, e.g. a background shell, so any replica's session list can show
  the session as working.
- ``background_work_reported_at`` (BigInteger): when the bound runner decided
  that value, in its own epoch nanoseconds, so a report that arrives late
  cannot overwrite a newer one.
- ``blocked_on`` (String(128)): why a running session's agent is parked on a
  terminal prompt, e.g. "permission prompt", so any replica's session list can
  show the session as needing input.

SQLite does not enforce declared types, so there, and on Cloudflare D1, which
serves SQLite, ``blocked_on`` also gets a CHECK bounding its stored bytes to 512
(128 characters at up to four bytes each), ``background_work_running`` a CHECK
that it holds 0 or 1, and ``background_work_reported_at`` a CHECK that it holds
an integer. Each column is added with its CHECK in one ``ADD COLUMN``, so the
table is not rebuilt.

Additive with no default or backfill. Deploy before the server that writes
them; older servers ignore these columns. Downgrade drops them, discarding the
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
_RUNNING_CHECK = "ck_conversation_metadata_running_boolean"
_REPORTED_AT_CHECK = "ck_conversation_metadata_reported_at_integer"
# Dialects that store a value of any type in any column.
_SQLITE_FAMILY = frozenset({"sqlite", "cloudflare_d1"})


def upgrade() -> None:
    """Add ``background_work_running``, ``background_work_reported_at`` and ``blocked_on``."""
    if op.get_bind().dialect.name in _SQLITE_FAMILY:
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN background_work_running BOOLEAN "
            f"CONSTRAINT {_RUNNING_CHECK} "
            "CHECK (background_work_running IS NULL OR background_work_running IN (0, 1))"
        )
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN background_work_reported_at BIGINT "
            f"CONSTRAINT {_REPORTED_AT_CHECK} "
            "CHECK (background_work_reported_at IS NULL "
            "OR typeof(background_work_reported_at) = 'integer')"
        )
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN blocked_on VARCHAR(128) "
            f"CONSTRAINT {_BLOCKED_ON_CHECK} "
            "CHECK (blocked_on IS NULL OR length(CAST(blocked_on AS BLOB)) <= 512)"
        )
    else:
        op.add_column(_TABLE, sa.Column("background_work_running", sa.Boolean(), nullable=True))
        op.add_column(
            _TABLE, sa.Column("background_work_reported_at", sa.BigInteger(), nullable=True)
        )
        op.add_column(_TABLE, sa.Column("blocked_on", sa.String(128), nullable=True))


def downgrade() -> None:
    """Remove ``background_work_running``, ``background_work_reported_at`` and ``blocked_on``."""
    with op.batch_alter_table(_TABLE) as batch_op:
        if op.get_bind().dialect.name in _SQLITE_FAMILY:
            # The rebuilt table would otherwise keep CHECKs on the dropped columns.
            batch_op.drop_constraint(_BLOCKED_ON_CHECK, type_="check")
            batch_op.drop_constraint(_REPORTED_AT_CHECK, type_="check")
            batch_op.drop_constraint(_RUNNING_CHECK, type_="check")
        batch_op.drop_column("blocked_on")
        batch_op.drop_column("background_work_reported_at")
        batch_op.drop_column("background_work_running")
