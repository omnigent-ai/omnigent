"""Merge the completed-state and pull-request schema branches.

Revision ID: b2e4f6a8c013
Revises: 9e82957bd115, d3f7a1c9e2b4

Both parent revisions remain unchanged. Upgrade and downgrade only reconcile
Alembic's version rows; the merge itself changes no application schema or data.
"""

from __future__ import annotations

revision: str = "b2e4f6a8c013"
down_revision: tuple[str, ...] = ("9e82957bd115", "d3f7a1c9e2b4")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Join the independently applied parent revisions."""


def downgrade() -> None:
    """Restore both parent version markers without undoing either schema."""
