"""merge mobile push heads after 27d1

efc019f4d3dd is already published; upstream's 27d16059fab9 grew from the
same ancestry, so the two heads are joined here instead.

Revision ID: 7fc806e94a38
Revises: efc019f4d3dd, 27d16059fab9
Create Date: 2026-11-14 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "7fc806e94a38"
down_revision: str | Sequence[str] | None = ("efc019f4d3dd", "27d16059fab9")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
