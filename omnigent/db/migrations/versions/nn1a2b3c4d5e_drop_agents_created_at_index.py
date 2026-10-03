"""drop the agents created_at index

Revision ID: nn1a2b3c4d5e
Revises: mm1a2b3c4d5e
Create Date: 2026-10-02 00:00:00.000000

Drops ``ix_agents_created_at``. Both agent listings walk
``ix_agents_kind_owner_created`` (server agents with ``created_by IS NULL``,
user agents by owner), so nothing reads it any more.

Deployment: ship this one release after the listing change, once no older
server is running. Deployments that name ``ix_agents_created_at`` in query
hints must switch them to ``ix_agents_kind_owner_created`` first; deployments
that apply schema outside alembic drop it themselves. On PostgreSQL the drop
runs ``CONCURRENTLY`` so writes to ``agents`` continue. Roll back by
downgrading this revision, which rebuilds the index.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "nn1a2b3c4d5e"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_agents_created_at"
_TABLE = "agents"
_COLUMNS = ["workspace_id", "created_at", "id"]


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        op.drop_index(_INDEX, table_name=_TABLE)
        return
    # CONCURRENTLY can't run in a transaction; autocommit lets writers proceed.
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_INDEX}")


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        op.create_index(_INDEX, _TABLE, _COLUMNS)
        return
    with op.get_context().autocommit_block():
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_INDEX} ON {_TABLE} ({', '.join(_COLUMNS)})"
        )
