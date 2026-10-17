"""merge composition heads after ll1

b7e41c0d92af shipped to production joining ii1a2b3c4d5e; it is immutable, so
the jj/kk/ll chain that later grew from ii1a2b3c4d5e is joined here instead.

Revision ID: ce67dc4f3baa
Revises: b7e41c0d92af, ll1a2b3c4d5e
Create Date: 2026-09-26 00:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "ce67dc4f3baa"
down_revision: str | Sequence[str] | None = (
    "b7e41c0d92af",
    "ll1a2b3c4d5e",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join the migration branches without additional DDL."""


def downgrade() -> None:
    """Split the migration branches without additional DDL."""
