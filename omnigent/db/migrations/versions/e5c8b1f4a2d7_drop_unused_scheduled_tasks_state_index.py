"""Drop the unused ix_scheduled_tasks_state index.

Revision ID: e5c8b1f4a2d7
Revises: f6d3b8a2c1e9
Create Date: 2026-07-20 00:00:00.000000

``ix_scheduled_tasks_state`` on ``scheduled_tasks``
(``workspace_id, state, created_at, id``) does not earn its keep. Its
per-workspace query shape (``WHERE workspace_id AND state ORDER BY created_at,
id``, i.e. ``list_active``) has no production caller; the scheduler reads active
tasks exactly once at boot via ``list_active_all_workspaces`` (``WHERE state
ORDER BY workspace_id, created_at, id``), which is a near-full scan regardless.

``ix_scheduled_tasks_created_at`` (``workspace_id, created_at, id``) already
serves that boot read: a scan of it yields the exact ``ORDER BY workspace_id,
created_at, id`` the query wants, with ``state`` applied as a residual filter.
The residual check is free here because the store selects whole rows, so
``state`` is already loaded; and ``scheduled_tasks`` is low-cardinality (a
handful of tasks per user, and ``delete`` is a hard delete so no ``deleted``
rows linger), leaving nothing meaningful to skip. So the index is pure
write/space overhead.

The ``state`` column and its ``ck_scheduled_tasks_state`` check constraint are
unchanged -- only the index is removed.

Index-only, no data change. ``DROP``/``CREATE INDEX`` is native on every
dialect (no table rebuild). Downgrade restores the index.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "e5c8b1f4a2d7"
down_revision: str | None = "f6d3b8a2c1e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_scheduled_tasks_state"
_TABLE = "scheduled_tasks"


def upgrade() -> None:
    """Drop the unused (workspace_id, state, created_at, id) index."""
    op.drop_index(_INDEX, table_name=_TABLE)


def downgrade() -> None:
    """Restore the (workspace_id, state, created_at, id) index."""
    op.create_index(_INDEX, _TABLE, ["workspace_id", "state", "created_at", "id"])
