"""Merge the workflow and automation schema branches.

Revision ID: e9b6a17c20d4
Revises: 9a2c7e6b4f10, 27d16059fab9

Both parent revisions remain unchanged. Upgrade and downgrade only reconcile
Alembic's version rows; the merge itself changes no application schema or data.
"""

from __future__ import annotations

revision: str = "e9b6a17c20d4"
down_revision: tuple[str, ...] = ("9a2c7e6b4f10", "27d16059fab9")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Join the independently applied parent revisions."""


def downgrade() -> None:
    """Restore both parent version markers without undoing either schema."""
