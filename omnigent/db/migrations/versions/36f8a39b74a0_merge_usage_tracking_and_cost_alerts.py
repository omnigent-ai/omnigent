"""Merge the usage-tracking and cost-alerts schema branches.

Revision ID: 36f8a39b74a0
Revises: a7d4c2e9f610, zb3c4d5e6f7a

Both parent revisions remain unchanged. Upgrade and downgrade only reconcile
Alembic's version rows; the merge itself changes no application schema or data.
"""

from __future__ import annotations

revision: str = "36f8a39b74a0"
down_revision: tuple[str, ...] = ("a7d4c2e9f610", "zb3c4d5e6f7a")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Join the independently applied parent revisions."""


def downgrade() -> None:
    """Restore both parent version markers without undoing either schema."""
