"""merge mobile push heads after ffee

8f1ffd29c77d is already published; upstream's ffeec66eadb5 grew from the
same ancestry, so the two heads are joined here instead.

Revision ID: 76f61aeb7833
Revises: 8f1ffd29c77d, ffeec66eadb5
Create Date: 2026-11-18 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "76f61aeb7833"
down_revision: str | Sequence[str] | None = ("8f1ffd29c77d", "ffeec66eadb5")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
