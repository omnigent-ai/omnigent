"""merge main's scheduled_tasks smart_routing migration

Revision ID: 10181aec0f14
Revises: 5ab01aaca219, 750fa7b2cd48
Create Date: 2026-11-20 13:20:13.040752
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "10181aec0f14"
down_revision: tuple[str, ...] = ("5ab01aaca219", "750fa7b2cd48")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
