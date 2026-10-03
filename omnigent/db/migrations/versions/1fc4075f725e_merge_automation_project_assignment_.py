"""merge automation-project-assignment with main

Revision ID: 1fc4075f725e
Revises: c18e2f7a4b90, hh1b2c3d4e5f
Create Date: 2026-09-17 12:53:51.654603
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "1fc4075f725e"
down_revision: str | Sequence[str] | None = ("c18e2f7a4b90", "hh1b2c3d4e5f")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
