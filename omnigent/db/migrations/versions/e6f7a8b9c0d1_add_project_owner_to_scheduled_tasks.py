"""add project_owner to scheduled_tasks

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-08-03 00:00:00.000001

Persists the ``ProjectStore`` owner scope a task's ``project_id`` was
validated and filed under, resolved ONCE at create/update time (see
``omnigent.server.auth.resolve_project_owner`` /
``encode_scheduled_task_project_owner``). Adds a nullable ``project_owner``
(``String(128)``, matching ``user_id``'s width) to ``scheduled_tasks``.

Why this column exists: the server's auth mode
(auth-disabled / ``OMNIGENT_LOCAL_SINGLE_USER=1`` / multi-user) can change
between when a task is created and when it later fires (e.g. across a
restart). ``scheduled_tasks.user_id`` alone can't tell a fire-time
``resolve_project_owner`` call which mode was active at CREATE time, so
re-resolving there risked treating a still-valid project as vanished after a
mode change. Persisting the resolved owner at create/update time removes
that re-resolution from the fire path entirely.

Migration note: rows with a ``project_id`` created before this column
existed have ``project_owner IS NULL``. The fire path
(``decode_scheduled_task_project_owner``) treats a NULL as "legacy — fall
back to re-resolving from the CURRENT auth mode", the same best-effort
behavior the feature had before this column was introduced. This is an
accepted, explicitly-handled gap for that one-time migration window, not
silently masked.

Additive. No foreign-key constraint (schema Rule R032): ``project_owner`` is
an opaque identity string (matching ``user_id``), not a row reference.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e6f7a8b9c0d1"
down_revision: str | None = "d5e6f7a8b9c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable ``project_owner`` column."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.add_column(sa.Column("project_owner", sa.String(128), nullable=True))


def downgrade() -> None:
    """Drop the ``project_owner`` column."""
    with op.batch_alter_table("scheduled_tasks") as batch_op:
        batch_op.drop_column("project_owner")
