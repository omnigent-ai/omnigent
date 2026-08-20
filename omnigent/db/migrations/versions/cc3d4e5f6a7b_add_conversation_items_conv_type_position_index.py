"""Add (workspace_id, conversation_id, type, position DESC) index on conversation_items.

Revision ID: cc3d4e5f6a7b
Revises: bb2c3d4e5f6a
Create Date: 2026-07-14 00:00:00.000000

Adds a composite index that backs the latest-message-preview query
(``list_latest_message_items_for_conversations`` /
``_ranked_latest_message_items``) powering the child-session sidebar:

    SELECT ... FROM conversation_items
    WHERE workspace_id = ? AND conversation_id IN (...) AND type = 'message'
    -- ranked per conversation by position DESC, top-N kept

The existing unique index ``(workspace_id, conversation_id, position)`` covers
the partition + order but not the ``type`` filter, so Postgres reads every
item in the matched conversations and rechecks ``type`` on the heap —
discarding the majority (messages are a minority of items in agent
transcripts). Ordering ``type`` before ``position`` lets the scan seek to
``(workspace_id, conversation_id, type)`` and walk ``position DESC`` directly.

Plain (non-partial) index so it builds identically on SQLite, PostgreSQL, and
MySQL — the codebase dropped partial indexes for MySQL compatibility in
``z5a2b3c4d5e6``. DESC ordering is expressed via ``sa.text`` because Alembic's
column list takes no per-column sort direction; all three dialects honor DESC
in a ``CREATE INDEX`` column list.

Index-only: ``CREATE INDEX`` / ``DROP INDEX`` are native on every dialect, so
no batch table-rebuild (and no SQLite ``foreign_keys`` guard) is needed.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "cc3d4e5f6a7b"
down_revision: str | None = "bb2c3d4e5f6a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_conversation_items_conv_type_position",
        "conversation_items",
        ["workspace_id", "conversation_id", "type", sa.text("position DESC")],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_items_conv_type_position",
        table_name="conversation_items",
    )
