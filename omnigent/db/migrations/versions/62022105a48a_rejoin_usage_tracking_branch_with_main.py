"""rejoin usage-tracking branch with main

Revision ID: 62022105a48a
Revises: b4e7f1a9c3d2, ffeec66eadb5
Create Date: 2026-11-18 09:20:41.662510
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "62022105a48a"
down_revision: str | None = ("b4e7f1a9c3d2", "ffeec66eadb5")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
