"""Add user_usage_summary table for pre-computed breakdowns.

Revision ID: zb3c4d5e6f7g
Revises: e5d9bc8ac650
Create Date: 2026-08-30 00:00:00.000000

Adds a new ``user_usage_summary`` table to store pre-aggregated usage breakdowns
per user (harness and model cost distributions). This table is maintained
incrementally as session usage is recorded, enabling O(1) breakdown queries
instead of O(N) full-session scans on every ``GET /v1/usage`` request.

Schema:
- ``user_id``: The user/owner identifier (TEXT primary key)
- ``harness_breakdown``: JSON text mapping harness names to total cost USD
- ``model_breakdown``: JSON text mapping model IDs to total cost USD
- ``needs_rebuild``: Boolean flag indicating cache is stale (defaults to true)
- ``total_sessions``: Count of sessions contributing to this summary
- ``last_updated_at``: Epoch seconds when last rebuilt

The ``needs_rebuild`` flag implements lazy cache invalidation: when any session
usage changes, we set ``needs_rebuild=True`` for that user. On the next
``GET /v1/usage`` request, if ``needs_rebuild=True``, we rebuild the breakdowns
from all sessions (O(N)) and cache them. If ``needs_rebuild=False``, we serve
the cached breakdowns (O(1)). Most requests will be O(1) since users don't
update usage constantly.

Breakdowns are all-time aggregates (not date-filtered). Date-range filters on
``/v1/usage`` still apply to the daily cost timeline and session list, but
breakdown charts show the user's overall cost distribution.

No backfill needed: existing users start with ``needs_rebuild=True`` and
populate on first request.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "zb3c4d5e6f7g"
down_revision: str | None = "e5d9bc8ac650"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create ``user_usage_summary`` table.

    Idempotent: skips table creation if it already exists, so re-running
    upgrade does not crash with ``relation already exists``.
    """
    inspector = sa.inspect(op.get_bind())
    if "user_usage_summary" not in inspector.get_table_names():
        op.create_table(
            "user_usage_summary",
            sa.Column(
                "workspace_id",
                sa.BigInteger(),
                primary_key=True,
                nullable=False,
                server_default="0",
            ),
            sa.Column("user_id", sa.String(255), primary_key=True, nullable=False),
            sa.Column(
                "harness_breakdown",
                sa.Text(),  # JSON stored as text for broad compatibility
                nullable=False,
                server_default="{}",
            ),
            sa.Column(
                "model_breakdown",
                sa.Text(),
                nullable=False,
                server_default="{}",
            ),
            sa.Column(
                "needs_rebuild",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            ),
            sa.Column("total_sessions", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_updated_at", sa.Integer(), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    """Drop ``user_usage_summary`` table."""
    op.drop_table("user_usage_summary")
