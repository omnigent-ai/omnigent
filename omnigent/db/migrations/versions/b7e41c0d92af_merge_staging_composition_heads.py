"""merge staging composition heads

Revision ID: b7e41c0d92af
Revises: 370db4f834a4, d29f3a8b5c01, ii1a2b3c4d5e
Create Date: 2026-09-22 08:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "b7e41c0d92af"
down_revision: str | Sequence[str] | None = (
    "370db4f834a4",
    "d29f3a8b5c01",
    "ii1a2b3c4d5e",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
