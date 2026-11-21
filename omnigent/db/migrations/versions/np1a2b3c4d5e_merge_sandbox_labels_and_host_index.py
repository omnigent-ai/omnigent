"""Merge optional sandbox labels and the conversation host index.

Revision ID: np1a2b3c4d5e
Revises: no1a2b3c4d5e, nf12ab34cd56
"""

from collections.abc import Sequence

revision: str = "np1a2b3c4d5e"
down_revision: tuple[str, str] = ("no1a2b3c4d5e", "nf12ab34cd56")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Both parents contain independent, additive schema changes."""


def downgrade() -> None:
    """Leave both parent revisions intact."""
