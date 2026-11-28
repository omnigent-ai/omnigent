"""Merge optional sandbox metadata with scheduled-task dispatch.

Revision ID: nq1a2b3c4d5e
Revises: np1a2b3c4d5e, c8e4f2a6b901
"""

revision = "nq1a2b3c4d5e"
down_revision = ("np1a2b3c4d5e", "c8e4f2a6b901")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Both parents are independent additive changes."""


def downgrade() -> None:
    """Keep both parents intact."""
