"""Merge child-spawn receipts and scheduled-task kinds.

Revision ID: c6e9a2b7d481
Revises: as1b2c3d4e5f, 27d16059fab9
"""

from __future__ import annotations

revision: str = "c6e9a2b7d481"
down_revision: tuple[str, str] = ("as1b2c3d4e5f", "27d16059fab9")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Keep both independent schema changes."""


def downgrade() -> None:
    """Leave schema reversal to the parent migrations."""
