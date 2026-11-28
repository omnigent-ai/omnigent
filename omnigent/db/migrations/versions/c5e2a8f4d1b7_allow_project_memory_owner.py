"""Allow a project to own memory.

Revision ID: c5e2a8f4d1b7
Revises: e3b7d1f5a9c2

Widens the ``memories.actor_type`` check from user (``1``) to user and project
(``1, 2``). No rows change, and the upgrade runs online.

Rollback: older binaries only read their own user owner (``1``), so project
rows are never decoded by them. A binary older than this revision refuses a
database at it, so roll back by downgrading first. The downgrade refuses while
any project memory exists; delete those rows (``actor_type = 2``) first.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "c5e2a8f4d1b7"
down_revision: str | None = "e3b7d1f5a9c2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "memories"
_PROJECT = 2


def upgrade() -> None:
    """Replace the owner check so it also accepts projects."""
    with op.batch_alter_table(_TABLE) as batch:
        # A distinct name lets CockroachDB replace the check in one transaction.
        batch.drop_constraint("ck_memories_actor_type", type_="check")
        batch.create_check_constraint("ck_memories_owner_type", "actor_type IN (1, 2)")


def downgrade() -> None:
    """Restore the user-only check, only when no project owns memory."""
    if context.is_offline_mode():
        raise RuntimeError("Downgrade requires a live connection to check memory owners")
    memories = sa.table(_TABLE, sa.column("actor_type"))
    owned = sa.select(memories.c.actor_type).where(memories.c.actor_type == _PROJECT).limit(1)
    if op.get_bind().execute(owned).first():
        raise RuntimeError("Delete project memory (actor_type = 2) before downgrading")
    with op.batch_alter_table(_TABLE) as batch:
        batch.drop_constraint("ck_memories_owner_type", type_="check")
        batch.create_check_constraint("ck_memories_actor_type", "actor_type IN (1)")
