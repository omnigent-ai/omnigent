"""merge mobile push heads after bf30

7df14f1e4697 is already published; upstream's bf30d7cbda61 grew from the
same ancestry, so the two heads are joined here instead.

Revision ID: 5cee91b68c65
Revises: 7df14f1e4697, bf30d7cbda61
Create Date: 2026-11-23 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "5cee91b68c65"
down_revision: str | Sequence[str] | None = ("7df14f1e4697", "bf30d7cbda61")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
