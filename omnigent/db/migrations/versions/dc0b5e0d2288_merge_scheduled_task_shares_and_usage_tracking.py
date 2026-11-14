"""Merge the scheduled-task-shares and usage-tracking schema branches.

Revision ID: dc0b5e0d2288
Revises: a8be64286ffc, 36f8a39b74a0

Main added the scheduled_task_shares table (a8be64286ffc) on the same parent
revision (a7d4c2e9f610) that this branch's earlier merge revision
(36f8a39b74a0) descends from, which left the script directory with two heads.
Both parent revisions remain unchanged. Upgrade and downgrade only reconcile
Alembic's version rows; the merge itself changes no application schema or
data.
"""

from __future__ import annotations

revision: str = "dc0b5e0d2288"
down_revision: tuple[str, ...] = ("a8be64286ffc", "36f8a39b74a0")
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Join the independently applied parent revisions."""


def downgrade() -> None:
    """Restore both parent version markers without undoing either schema."""
