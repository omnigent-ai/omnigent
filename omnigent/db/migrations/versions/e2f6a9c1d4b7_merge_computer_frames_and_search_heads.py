"""Merge the Computer Use file-purpose and conversation-search branches.

Revision ID: e2f6a9c1d4b7
Revises: a8c4e1f6b2d9, d5e9f1a2b3c4
Create Date: 2026-08-11
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "e2f6a9c1d4b7"
down_revision: tuple[str, str] = ("a8c4e1f6b2d9", "d5e9f1a2b3c4")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the two schema branches without additional changes."""


def downgrade() -> None:
    """Split back to the two parent migration heads."""
