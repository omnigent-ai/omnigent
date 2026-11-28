"""merge mobile push heads after 5ab0

506c62d89f0c is already published; upstream's 5ab01aaca219 grew from the
same ancestry, so the two heads are joined here instead.

Revision ID: 7df14f1e4697
Revises: 506c62d89f0c, 5ab01aaca219
Create Date: 2026-11-21 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "7df14f1e4697"
down_revision: str | Sequence[str] | None = ("506c62d89f0c", "5ab01aaca219")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
