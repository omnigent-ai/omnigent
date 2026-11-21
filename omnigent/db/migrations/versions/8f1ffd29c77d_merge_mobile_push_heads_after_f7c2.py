"""merge mobile push heads after f7c2

7fc806e94a38 is already published; upstream's f7c2d8a4b901 grew from the
same ancestry, so the two heads are joined here instead.

Revision ID: 8f1ffd29c77d
Revises: 7fc806e94a38, f7c2d8a4b901
Create Date: 2026-11-15 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "8f1ffd29c77d"
down_revision: str | Sequence[str] | None = ("7fc806e94a38", "f7c2d8a4b901")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
