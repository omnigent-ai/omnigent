"""merge mobile push heads after a7d4

c4e81b7a2f90 is already published; upstream's a7d4c2e9f610 grew from the
same ancestry, so the two heads are joined here instead.

Revision ID: 52dd36c1a9b9
Revises: c4e81b7a2f90, a7d4c2e9f610
Create Date: 2026-11-11 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "52dd36c1a9b9"
down_revision: str | Sequence[str] | None = ("c4e81b7a2f90", "a7d4c2e9f610")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
