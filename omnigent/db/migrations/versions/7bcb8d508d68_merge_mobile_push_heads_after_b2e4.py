"""merge mobile push heads after b2e4

a24417751342 is already published; upstream's b2e4f6a8c013 grew from the
same ancestry, so the two heads are joined here instead.

Revision ID: 7bcb8d508d68
Revises: a24417751342, b2e4f6a8c013
Create Date: 2026-10-27 05:45:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "7bcb8d508d68"
down_revision: str | Sequence[str] | None = ("a24417751342", "b2e4f6a8c013")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
