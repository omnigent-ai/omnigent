"""Add (workspace_id, host_id) index on omnigent_conversation_metadata.

Revision ID: f1a7c3e9b204
Revises: e5d9bc8ac650
Create Date: 2026-08-26 00:00:00.000000

Backs the shared-worktree check that gates worktree removal on session
delete:

    SELECT id FROM omnigent_conversation_metadata
    WHERE workspace_id = ? AND host_id = ? AND workspace = ? AND id <> ?
    LIMIT 32

Before this index the planner fell through to
``ix_conversation_metadata_project_id`` and walked every one of the
workspace's metadata rows, rechecking ``host_id`` / ``workspace`` on each.
Measured on SQLite with 20k conversations: 7.1ms, versus 2.1ms seeking
``(workspace_id, host_id)`` and filtering only that host's rows. The delete
request itself measures ~8ms, so the unindexed scan was the dominant cost.

``workspace`` is deliberately left out of the key. It is ``VARCHAR(2048)``,
which exceeds MySQL's 3072-byte index-key limit under utf8mb4 and would make
this migration fail outright there. A digest column (the
``policy_name_cksum`` pattern) is the way to get a full seek if profiling
ever calls for it.

Plain (non-partial) index so it builds identically on SQLite, PostgreSQL, and
MySQL — the codebase dropped partial indexes for MySQL compatibility in
``z5a2b3c4d5e6``. A partial index would not have been used here anyway: the
query carries no ``git_branch IS NOT NULL`` predicate, and adding one would
wrongly skip sessions that share the directory without a worktree of their
own.

Index-only: ``CREATE INDEX`` / ``DROP INDEX`` are native on every dialect, so
no batch table-rebuild (and no SQLite ``foreign_keys`` guard) is needed.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f1a7c3e9b204"
down_revision: str | None = "e5d9bc8ac650"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_conversation_metadata_host_id",
        "omnigent_conversation_metadata",
        ["workspace_id", "host_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_conversation_metadata_host_id",
        table_name="omnigent_conversation_metadata",
    )
