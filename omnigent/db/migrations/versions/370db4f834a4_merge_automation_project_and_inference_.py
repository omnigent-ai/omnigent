"""merge automation project and inference snapshot heads

Revision ID: 370db4f834a4
Revises: 1fc4075f725e, hi1b2c3d4e5f
Create Date: 2026-09-19 09:37:34.941613
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "370db4f834a4"
down_revision: str | Sequence[str] | None = ("1fc4075f725e", "hi1b2c3d4e5f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
