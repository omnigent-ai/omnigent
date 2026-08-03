"""Add an (archived, created_at) ordering index on conversations.

Revision ID: za1b2c3d4e5f6
Revises: b3c4d5e6f7a8
Create Date: 2026-07-23 00:00:00.000000

The sessions list defaults to ``created_at DESC`` ordering. With the ACL
filter expressed as a correlated EXISTS (no more ``id IN (...)`` resolving
through the PK), the default listing needs an order-compatible index or
every page pays a scan-and-sort over the workspace's active rows. Mirrors
``ix_conversations_archived_updated``, which already serves the
``updated_at`` sort for the sidebar.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "za1b2c3d4e5f6"
down_revision: str | None = "b3c4d5e6f7a8"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_INDEX_NAME = "ix_conversations_archived_created"


def upgrade() -> None:
    op.create_index(
        _INDEX_NAME,
        "conversations",
        ["workspace_id", "archived", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index(_INDEX_NAME, table_name="conversations")
