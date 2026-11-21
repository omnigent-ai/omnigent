"""merge mobile push heads after f9b3

9bb7aaefed93 is already published; upstream's f9b3e6c2a401 grew from the
same ancestry, so the two heads are joined here instead.

Revision ID: c4e81b7a2f90
Revises: 9bb7aaefed93, f9b3e6c2a401
Create Date: 2026-11-10 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "c4e81b7a2f90"
down_revision: str | Sequence[str] | None = ("9bb7aaefed93", "f9b3e6c2a401")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
