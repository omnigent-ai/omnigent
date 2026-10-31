"""Merge optional sandbox metadata with the completed-state schema.

Revision ID: nr1a2b3c4d5e
Revises: nq1a2b3c4d5e, b2e4f6a8c013
"""

revision = "nr1a2b3c4d5e"
down_revision = ("nq1a2b3c4d5e", "b2e4f6a8c013")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join the parent revisions without changing their schema or data."""


def downgrade() -> None:
    """Keep both parents intact."""
