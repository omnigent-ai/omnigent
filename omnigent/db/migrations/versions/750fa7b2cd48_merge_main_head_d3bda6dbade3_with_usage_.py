"""merge main head d3bda6dbade3 with usage-tracking joint head

Revision ID: 750fa7b2cd48
Revises: 62022105a48a, d3bda6dbade3
Create Date: 2026-11-19 09:20:14.998677
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "750fa7b2cd48"
down_revision: tuple[str, ...] = ("62022105a48a", "d3bda6dbade3")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
