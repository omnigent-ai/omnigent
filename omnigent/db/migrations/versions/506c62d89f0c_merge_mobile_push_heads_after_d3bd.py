"""merge mobile push heads after d3bd

76f61aeb7833 is already published; upstream's d3bda6dbade3 grew from the
same ancestry, so the two heads are joined here instead.

Revision ID: 506c62d89f0c
Revises: 76f61aeb7833, d3bda6dbade3
Create Date: 2026-11-19 12:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "506c62d89f0c"
down_revision: str | Sequence[str] | None = ("76f61aeb7833", "d3bda6dbade3")
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
