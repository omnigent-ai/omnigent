"""Add the canonical main-session pointer with a read-only legacy fallback.

Revision ID: 4e5f6a7b8c9d
Revises: 3d4e5f6a7b8c

Ship this additive schema separately before the application. Existing binaries
continue to use orchestrator_session_id; upgraded readers fall back to it until
the first explicit main-session assignment. New writers only write main_session_id.
Do not mix old/new main-chat writers after enabling replacement in the new UI.
Retain the legacy column through the 0.25 release and a two-week observation
window. A later schema-only migration may remove it after bounded backfill and
reader retirement. No history or session rows are rewritten here.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "4e5f6a7b8c9d"
down_revision: str | None = "3d4e5f6a7b8c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    if "main_session_id" not in {
        column["name"] for column in sa.inspect(op.get_bind()).get_columns("projects")
    }:
        op.add_column("projects", sa.Column("main_session_id", Uuid16(), nullable=True))


def downgrade() -> None:
    # Retaining an unused additive column is safe for old binaries and preserves writes.
    pass
