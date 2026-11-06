"""merge mobile push heads after f1c7

cd0734535d1f is already published; upstream's f1c7a9e2b406 grew from the
same ancestry, so the two heads are joined here instead.

Revision ID: 9bb7aaefed93
Revises: cd0734535d1f, f1c7a9e2b406
Create Date: 2026-11-06 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "9bb7aaefed93"
down_revision: str | Sequence[str] | None = ("cd0734535d1f", "f1c7a9e2b406")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
