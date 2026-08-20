"""Consolidate scheduled_tasks listing into one user-scoped index.

Revision ID: f4664ca64ea8
Revises: c2d3e4f5a6b7
Create Date: 2026-07-21 00:00:00.000000

``scheduled_tasks`` carried two secondary indexes:

- ``ix_scheduled_tasks_created_at`` (``workspace_id, created_at, id``)
- ``ix_scheduled_tasks_user_id``    (``workspace_id, user_id, id``)

The only per-request read is "list a user's tasks" (``GET /scheduled-tasks``):
``WHERE workspace_id AND user_id ORDER BY created_at, id``. The user index was
never used for it — the route filtered ``user_id`` in Python — so it was pure
write/space overhead; and the ``created_at`` index served only the sort, forcing
a scan of every owner's rows in the workspace with ``user_id`` as a residual
filter.

Replace both with a single ``ix_scheduled_tasks_user_scope``
(``workspace_id, user_id, created_at, id``). With the ``user_id`` filter pushed
into SQL, this is a covered seek for the per-user listing: it reads only the
caller's rows, already in ``created_at, id`` order (no filesort). The
scheduler-boot read (``list_active_all_workspaces``: ``WHERE state ORDER BY
workspace_id, created_at, id``) uses neither index for its ``state`` filter and
is a near-full scan once per process start regardless; its ordering only feeds
independent per-task timer arming, so scanning unordered costs nothing.

Index-only, no data change. ``DROP``/``CREATE INDEX`` is native on every
dialect (no table rebuild). Downgrade restores the two original indexes.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f4664ca64ea8"
down_revision: str | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "scheduled_tasks"
_MERGED = "ix_scheduled_tasks_user_scope"
_CREATED_AT = "ix_scheduled_tasks_created_at"
_USER_ID = "ix_scheduled_tasks_user_id"


def upgrade() -> None:
    """Fold the created_at + user_id indexes into one user-scoped index."""
    op.drop_index(_USER_ID, table_name=_TABLE)
    op.drop_index(_CREATED_AT, table_name=_TABLE)
    op.create_index(_MERGED, _TABLE, ["workspace_id", "user_id", "created_at", "id"])


def downgrade() -> None:
    """Restore the separate created_at and user_id indexes."""
    op.drop_index(_MERGED, table_name=_TABLE)
    op.create_index(_CREATED_AT, _TABLE, ["workspace_id", "created_at", "id"])
    op.create_index(_USER_ID, _TABLE, ["workspace_id", "user_id", "id"])
