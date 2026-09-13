"""Drop the unused ix_conversation_metadata_kind index.

Revision ID: f6d3b8a2c1e9
Revises: b7e4d2c9a1f3
Create Date: 2026-07-20 00:00:00.000000

``ix_conversation_metadata_kind`` on ``omnigent_conversation_metadata``
(``workspace_id, kind, id``) has no serving query. ``kind`` is fully determined
by ``parent_conversation_id`` nullness — a child always has a parent, a
top-level session never does — so ``list_conversations`` expresses the kind
filter on the AP ``conversations`` table (parent-nullness) and the sub-agent
roll-up (``list_child_conversation_ids_by_parent``) rides
``idx_conversations_parent``; neither reads the metadata ``kind`` column. It is
also a 2-value column (``kind IN (1, 2)``), so a standalone index could never be
selective.

So the index is pure write/space overhead. The ``kind`` column and its
``ck_conversation_metadata_kind`` check constraint are unchanged — only the index
is removed.

Index-only, no data change. ``DROP``/``CREATE INDEX`` is native on every
dialect (no table rebuild). Downgrade restores the index.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f6d3b8a2c1e9"
down_revision: str | None = "b7e4d2c9a1f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_conversation_metadata_kind"
_TABLE = "omnigent_conversation_metadata"


def upgrade() -> None:
    """Drop the unused (workspace_id, kind, id) index."""
    op.drop_index(_INDEX, table_name=_TABLE)


def downgrade() -> None:
    """Restore the (workspace_id, kind, id) index."""
    op.create_index(_INDEX, _TABLE, ["workspace_id", "kind", "id"])
