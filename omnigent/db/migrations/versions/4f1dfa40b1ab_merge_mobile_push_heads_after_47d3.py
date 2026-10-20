"""merge mobile push heads after 47d3

5cee91b68c65 already joins the mobile push branch; upstream's 47d3e8fc3e25
grew from bf30d7cbda61 alongside it, so the two heads are joined here.

Revision ID: 4f1dfa40b1ab
Revises: 5cee91b68c65, 47d3e8fc3e25
Create Date: 2026-11-24 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "4f1dfa40b1ab"
down_revision: str | Sequence[str] | None = ("5cee91b68c65", "47d3e8fc3e25")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
