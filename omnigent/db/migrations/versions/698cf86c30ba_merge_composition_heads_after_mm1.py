"""merge composition heads after mm1

f5b9e1a3d2c4 is already published; upstream's mm1a2b3c4d5e grew from the
same ancestry, so the two heads are joined here instead.

Revision ID: 698cf86c30ba
Revises: f5b9e1a3d2c4, mm1a2b3c4d5e
Create Date: 2026-10-03 18:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "698cf86c30ba"
down_revision: str | Sequence[str] | None = ("f5b9e1a3d2c4", "mm1a2b3c4d5e")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
