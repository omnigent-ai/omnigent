"""Merge scheduled-task and pull-request metadata migrations.

Revision ID: f2b7c4d9a601
Revises: 9e82957bd115, d3f7a1c9e2b4
"""

from __future__ import annotations

revision: str = "f2b7c4d9a601"
down_revision: tuple[str, str] = ("9e82957bd115", "d3f7a1c9e2b4")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Join the existing heads without changing schema or data."""


def downgrade() -> None:
    """Restore the two parent heads without changing schema or data."""
