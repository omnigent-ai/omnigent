"""Add session attention columns to conversation metadata.

Revision ID: 7b8c9d0e1f2a
Revises: c4f7a2e91b3d

Adds two nullable columns to ``omnigent_conversation_metadata``, beside the
other live-state columns so their writes cannot bump
``conversations.updated_at``:

- ``attention_pending`` (Boolean): a person wrote to the session and the turn
  answering them has not ended yet.
- ``attention_at`` (BigInteger): epoch seconds the session last produced
  something its owner should look at: the end of a turn a person started, or a
  request for human input. The Inbox lists a session only when this is newer
  than the viewer's last visit, so automatic turns stay out of it.

SQLite does not enforce declared types, so there, and on Cloudflare D1, each
column gets a CHECK in its ``ADD COLUMN``, so the table is not rebuilt.

Additive with no default or backfill. Older servers ignore these columns.
Downgrade drops them; the Inbox then lists only sessions that need attention
after the next person-started turn.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "7b8c9d0e1f2a"
down_revision: str | None = "c4f7a2e91b3d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "omnigent_conversation_metadata"
_PENDING_CHECK = "ck_conversation_metadata_attention_pending_boolean"
_ATTENTION_AT_CHECK = "ck_conversation_metadata_attention_at_integer"
# Dialects that store a value of any type in any column.
_SQLITE_FAMILY = frozenset({"sqlite", "cloudflare_d1"})


def upgrade() -> None:
    """Add ``attention_pending`` and ``attention_at``."""
    if op.get_bind().dialect.name in _SQLITE_FAMILY:
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN attention_pending BOOLEAN "
            f"CONSTRAINT {_PENDING_CHECK} "
            "CHECK (attention_pending IS NULL OR attention_pending IN (0, 1))"
        )
        op.execute(
            f"ALTER TABLE {_TABLE} ADD COLUMN attention_at BIGINT "
            f"CONSTRAINT {_ATTENTION_AT_CHECK} "
            "CHECK (attention_at IS NULL OR typeof(attention_at) = 'integer')"
        )
    else:
        op.add_column(_TABLE, sa.Column("attention_pending", sa.Boolean(), nullable=True))
        op.add_column(_TABLE, sa.Column("attention_at", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    """Remove ``attention_pending`` and ``attention_at``."""
    with op.batch_alter_table(_TABLE) as batch_op:
        if op.get_bind().dialect.name in _SQLITE_FAMILY:
            # The rebuilt table would otherwise keep CHECKs on the dropped columns.
            batch_op.drop_constraint(_ATTENTION_AT_CHECK, type_="check")
            batch_op.drop_constraint(_PENDING_CHECK, type_="check")
        batch_op.drop_column("attention_at")
        batch_op.drop_column("attention_pending")
