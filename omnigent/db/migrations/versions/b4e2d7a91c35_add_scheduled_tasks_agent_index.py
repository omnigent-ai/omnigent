"""add an index on scheduled_tasks by agent

Revision ID: b4e2d7a91c35
Revises: fcc79fe2b985
Create Date: 2026-11-10 00:00:00.000000

Adds ``ix_scheduled_tasks_agent`` on ``(workspace_id, agent_id, id)`` so the
tasks bound to one agent can be read across owners in bounded, seekable pages,
for example every user's proactive-agent task. Adding an index changes no
rows or reads that existing code depends on, so the migration can deploy
before the code that uses it and be rolled back by downgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b4e2d7a91c35"
down_revision: str | None = "fcc79fe2b985"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_scheduled_tasks_agent"


def upgrade() -> None:
    op.create_index(_INDEX, "scheduled_tasks", ["workspace_id", "agent_id", "id"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="scheduled_tasks")
