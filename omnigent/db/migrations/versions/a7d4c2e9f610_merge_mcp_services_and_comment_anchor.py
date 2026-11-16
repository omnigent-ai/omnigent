"""Merge the scheduled-task MCP services and comment anchor schema branches.

Revision ID: a7d4c2e9f610
Revises: f2c6a9d1b3e8, f9b3e6c2a401

Both parent revisions remain unchanged. Upgrade and downgrade only reconcile
Alembic's version rows; the merge itself changes no application schema or data.
"""

from __future__ import annotations

revision: str = "a7d4c2e9f610"
down_revision: tuple[str, ...] = ("f2c6a9d1b3e8", "f9b3e6c2a401")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Join the independently applied parent revisions."""


def downgrade() -> None:
    """Restore both parent version markers without undoing either schema."""
