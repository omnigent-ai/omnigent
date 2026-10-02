"""merge mobile push heads after d0a1

7bcb8d508d68 is already published; upstream's d0a1f879a01d grew from the
same ancestry, so the two heads are joined here instead.

Revision ID: 9d2840455e3b
Revises: 7bcb8d508d68, d0a1f879a01d
Create Date: 2026-10-28 15:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "9d2840455e3b"
down_revision: str | Sequence[str] | None = ("7bcb8d508d68", "d0a1f879a01d")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
