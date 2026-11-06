"""merge mobile push heads after c7cd

9d2840455e3b is already published; upstream's c7cd88f24c1c grew from the
same ancestry, so the two heads are joined here instead.

Revision ID: cd0734535d1f
Revises: 9d2840455e3b, c7cd88f24c1c
Create Date: 2026-11-05 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "cd0734535d1f"
down_revision: str | Sequence[str] | None = ("9d2840455e3b", "c7cd88f24c1c")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
