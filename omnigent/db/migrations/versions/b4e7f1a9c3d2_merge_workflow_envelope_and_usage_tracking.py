"""Merge the workflow-description-envelope and usage-tracking schema branches.

Revision ID: b4e7f1a9c3d2
Revises: f7c2d8a4b901, dc0b5e0d2288

Main added the workflow description envelope migration (f7c2d8a4b901) on
revision e9b6a17c20d4 while this branch's joint head (dc0b5e0d2288) already
descended from a different parent, which left the script directory with two
heads. Both parent revisions remain unchanged. Upgrade and downgrade only
reconcile Alembic's version rows; the merge itself changes no application
schema or data.
"""

from __future__ import annotations

revision: str = "b4e7f1a9c3d2"
down_revision: tuple[str, ...] = ("f7c2d8a4b901", "dc0b5e0d2288")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Join the independently applied parent revisions."""


def downgrade() -> None:
    """Restore both parent version markers without undoing either schema."""
