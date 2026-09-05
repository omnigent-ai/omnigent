"""Merge the scheduled-project and managed-sandbox migration heads.

Revision ID: c18e2f7a4b90
Revises: a5363b7c9d2e, gb1b2c3d4e5f

The scheduled-project revision shipped downstream before the managed-sandbox
revision landed on main. Keep both revisions as siblings so databases already
stamped at ``a5363b7c9d2e`` still run ``gb1b2c3d4e5f`` on their next upgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "c18e2f7a4b90"
down_revision: str | Sequence[str] | None = (
    "a5363b7c9d2e",
    "gb1b2c3d4e5f",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the two schema branches without additional DDL."""


def downgrade() -> None:
    """Split the migration heads without additional DDL."""
