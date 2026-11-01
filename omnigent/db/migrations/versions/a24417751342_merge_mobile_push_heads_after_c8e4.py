"""merge mobile push heads after c8e4

78b6d503592b is already published; upstream's c8e4f2a6b901 grew from the
same ancestry, so the two heads are joined here instead.

Revision ID: a24417751342
Revises: 78b6d503592b, c8e4f2a6b901
Create Date: 2026-10-22 21:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "a24417751342"
down_revision: str | Sequence[str] | None = ("78b6d503592b", "c8e4f2a6b901")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
