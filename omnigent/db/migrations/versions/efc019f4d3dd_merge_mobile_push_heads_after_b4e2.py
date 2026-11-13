"""merge mobile push heads after b4e2

52dd36c1a9b9 is already published; upstream's b4e2d7a91c35 grew from the
same ancestry, so the two heads are joined here instead.

Revision ID: efc019f4d3dd
Revises: 52dd36c1a9b9, b4e2d7a91c35
Create Date: 2026-11-13 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "efc019f4d3dd"
down_revision: str | Sequence[str] | None = ("52dd36c1a9b9", "b4e2d7a91c35")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
