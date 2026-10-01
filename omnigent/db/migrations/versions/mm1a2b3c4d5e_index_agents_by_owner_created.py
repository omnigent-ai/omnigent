"""index agents by kind, owner, and creation order

Revision ID: mm1a2b3c4d5e
Revises: ll1a2b3c4d5e
Create Date: 2026-10-01 00:00:00.000000

Adds ``ix_agents_kind_owner_created`` on ``(workspace_id, kind, created_by,
created_at, id)`` so template agents can be listed per owner with bounded
keyset pagination: one walk for operator templates (``created_by`` NULL) and
one for the viewer's own. ``kind`` leads so the walk skips session-scoped rows,
which outnumber templates by one per session.

Deployment: additive and safe to apply before or after the application
release that lists owned templates; older application code ignores it. Roll
back by downgrading this revision. Deployments that apply schema outside
alembic must create the same index.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "mm1a2b3c4d5e"
down_revision: str | None = "ll1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_agents_kind_owner_created"
_TABLE = "agents"


def upgrade() -> None:
    op.create_index(_INDEX, _TABLE, ["workspace_id", "kind", "created_by", "created_at", "id"])


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
