"""Add each session's append-only journal.

Revision ID: 5d872cd6632a
Revises: a7d4c2e9f610

``session_journal_entries`` holds what a session's agent plans and records:
plans, step statuses, notes and, later, the work it sends to other sessions.
Entries are append-only and are deleted only with their session.
``session_journal_heads`` holds one counter row per session; appends lock it
to allocate positions, so journal writes never wait on the session's metadata
row.

The primary key is ``(workspace_id, conversation_id, position, created_at)``,
and every index ends with ``position, created_at``. Keeping ``created_at`` in
the primary key, with no unique key that leaves it out, lets a deployment
partition the table by time with pure DDL. So the heads lock, not the
database, keeps positions and idempotency keys unique, as the conversation
lock does for conversation items. The payload is an envelope private to the
journal store; a CHECK caps it at 16,383 stored bytes. ``kind`` has no CHECK,
so adding a kind needs no migration. The tables have no database defaults:
writes name the workspace. ``workflow_id`` is nullable and unindexed, and
nothing writes it yet; it ships with the table so the phase that gives
workflows IDs needs no migration.

Deployment: apply this migration before a server that uses the tables.
Servers that migrate at startup do so on their own. Processes already running
an older build keep working on the migrated schema, since they never touch the
new tables. An older build does not start on it, though: startup refuses a
schema revision the build does not know.

Rollback: stop the servers, downgrade with a build that includes this
migration (``OMNIGENT_DB_URL=<uri> alembic -c omnigent/db/alembic.ini
downgrade a7d4c2e9f610``), then start the older build. Downgrade drops both
tables and every journal entry.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "5d872cd6632a"
down_revision: str | None = "a7d4c2e9f610"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None

# Mirrors omnigent.db.db_models.JOURNAL_PAYLOAD_MAX_STORED_BYTES at this revision.
_PAYLOAD_MAX_STORED_BYTES = 16_383

_ENTRIES = "session_journal_entries"
_HEADS = "session_journal_heads"
# Every index ends with the primary key's (position, created_at) suffix.
_INDEXES = {
    "ix_session_journal_entries_id": ["workspace_id", "conversation_id", "id"],
    "ix_session_journal_entries_idempotency_key": [
        "workspace_id",
        "conversation_id",
        "idempotency_key",
    ],
    "ix_session_journal_entries_kind": ["workspace_id", "conversation_id", "kind"],
    "ix_session_journal_entries_kind_step": ["workspace_id", "conversation_id", "kind", "step"],
    "ix_session_journal_entries_scheduled_task_run_id": [
        "workspace_id",
        "scheduled_task_run_id",
        "conversation_id",
        "kind",
    ],
    "ix_session_journal_entries_step": ["workspace_id", "conversation_id", "step"],
}


def upgrade() -> None:
    """Create ``session_journal_entries`` with its indexes, and ``session_journal_heads``."""
    op.create_table(
        _ENTRIES,
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("scheduled_task_run_id", Uuid16(), nullable=True),
        sa.Column("workflow_id", Uuid16(), nullable=True),
        sa.Column("step", sa.String(64), nullable=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id", "position", "created_at"),
        sa.CheckConstraint(
            f"length(payload) <= {_PAYLOAD_MAX_STORED_BYTES}",
            name="ck_session_journal_entries_payload_size",
        ),
    )
    for name, columns in _INDEXES.items():
        op.create_index(name, _ENTRIES, [*columns, "position", "created_at"])
    op.create_table(
        _HEADS,
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("next_position", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id"),
    )


def downgrade() -> None:
    """Drop both journal tables and every entry."""
    op.drop_table(_HEADS)
    for name in reversed(_INDEXES):
        op.drop_index(name, table_name=_ENTRIES)
    op.drop_table(_ENTRIES)
