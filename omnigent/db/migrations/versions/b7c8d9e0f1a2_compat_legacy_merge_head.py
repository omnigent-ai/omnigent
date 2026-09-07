"""Keep databases created by the previous merged migration line usable."""

from __future__ import annotations

from collections.abc import Sequence


def upgrade() -> None:
    """No-op compatibility bridge."""


def downgrade() -> None:
    """No-op compatibility bridge."""


revision: str = "b7c8d9e0f1a2"
down_revision: str | Sequence[str] | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None
