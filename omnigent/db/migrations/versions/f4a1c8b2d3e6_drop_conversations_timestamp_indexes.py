"""Drop the redundant conversations created_at / updated_at indexes.

Revision ID: f4a1c8b2d3e6
Revises: d1e2f3a4b5c6
Create Date: 2026-07-20 00:00:00.000000

The two bare sort indexes on ``conversations`` no longer earn their write cost:

- ``ix_conversations_created_at`` (``workspace_id, created_at, id``)
- ``ix_conversations_updated_at`` (``workspace_id, updated_at, id``)

Every path that sorts these columns already narrows the rows by something with
a better index. The sessions list is ACL-scoped, so it filters ``id IN (...)``
and resolves through the primary key ``(workspace_id, id)``; the default sidebar
(``archived = false`` sorted by ``updated_at DESC``) is served by
``ix_conversations_archived_updated``; and the sub-agent / root listings filter
on ``parent_conversation_id`` / ``root_conversation_id`` and use their own
indexes. So neither bare index is the chosen access path, while ``updated_at``
is rewritten on every item append — pure write amplification.

Index-only: no columns change, so no table rebuild is needed. ``DROP INDEX`` is
native on every dialect. Downgrade recreates both composite indexes exactly.
"""

from __future__ import annotations

from alembic import op

revision: str = "f4a1c8b2d3e6"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Drop the two redundant timestamp sort indexes."""
    op.drop_index("ix_conversations_created_at", table_name="conversations")
    op.drop_index("ix_conversations_updated_at", table_name="conversations")


def downgrade() -> None:
    """Recreate the composite ``(workspace_id, <ts>, id)`` sort indexes."""
    op.create_index(
        "ix_conversations_created_at",
        "conversations",
        ["workspace_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_conversations_updated_at",
        "conversations",
        ["workspace_id", "updated_at", "id"],
        unique=False,
    )
