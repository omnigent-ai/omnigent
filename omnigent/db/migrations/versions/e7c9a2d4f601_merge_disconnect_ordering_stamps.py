"""Merge disconnect ordering stamps with the current metadata schema.

Revision ID: e7c9a2d4f601
Revises: b2e4f6a8c013, ng12ab34cd56
"""

from __future__ import annotations

revision: str = "e7c9a2d4f601"
down_revision: tuple[str, ...] = ("b2e4f6a8c013", "ng12ab34cd56")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Join both applied schema branches without changing their data."""


def downgrade() -> None:
    """Restore both parent version markers without changing their data."""
