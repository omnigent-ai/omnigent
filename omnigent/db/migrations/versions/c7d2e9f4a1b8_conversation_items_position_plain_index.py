"""Make the conversation_items position index plain (drop UNIQUE + created_at).

Revision ID: c7d2e9f4a1b8
Revises: a2b7c3d8e4f9
Create Date: 2026-07-20 00:00:00.000000

``ix_conversation_items_conversation_id_position`` was UNIQUE on
``(workspace_id, conversation_id, position, created_at)``. The ``created_at``
tail only existed because a UNIQUE index must contain the partition key — and
with it in the key the DB no longer enforced position uniqueness anyway (only
per epoch-second). Strict position uniqueness is owned entirely by the
application: the ``next_position`` counter advanced under ``_lock_conversation``
never reuses a position, and no code path catches a position IntegrityError.

So the UNIQUE flag is redundant. This repoints the index to a plain
``(workspace_id, conversation_id, position)``:

- Same access path for the dominant per-conversation position-ordered scan.
- One less uniqueness probe on the hot conversation_items insert path.
- ``created_at`` is dropped: a non-unique index needs no partition key, so it
  is a local index on either engine. The PK still carries ``created_at``, so
  the table stays partition-ready.

Index-only, no data change. ``DROP``/``CREATE INDEX`` is native on every
dialect (no table rebuild). Downgrade restores the UNIQUE + ``created_at`` shape.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "c7d2e9f4a1b8"
down_revision: str | None = "a2b7c3d8e4f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_conversation_items_conversation_id_position"
_TABLE = "conversation_items"


def upgrade() -> None:
    """Swap the UNIQUE (…, position, created_at) index for a plain (…, position)."""
    op.drop_index(_INDEX, table_name=_TABLE)
    op.create_index(
        _INDEX,
        _TABLE,
        ["workspace_id", "conversation_id", "position"],
        unique=False,
    )


def downgrade() -> None:
    """Restore the UNIQUE (…, position, created_at) partition-ready index."""
    op.drop_index(_INDEX, table_name=_TABLE)
    op.create_index(
        _INDEX,
        _TABLE,
        ["workspace_id", "conversation_id", "position", "created_at"],
        unique=True,
    )
