"""Add durable receipts for scheduled-task mutations.

Revision ID: b4e2a8d06f19
Revises: d0a1f879a01d

Deploy before the request-id application change. Existing binaries ignore this
new table. Roll back the application independently and retain receipts; dropping
receipts after writes have started removes retry protection.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY

from omnigent.db.db_models import Uuid16

revision: str = "b4e2a8d06f19"
down_revision: str | None = "d0a1f879a01d"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create receipts, keyed by workspace, account identity and caller request."""
    op.create_table(
        "scheduled_task_requests",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("owner_key", Uuid16(), nullable=False),
        sa.Column("request_id", Uuid16(), nullable=False),
        sa.Column(
            "request_hash", sa.LargeBinary(32).with_variant(BINARY(32), "mysql"), nullable=False
        ),
        sa.Column("scheduled_task_id", Uuid16(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "owner_key", "request_id"),
        sa.CheckConstraint("length(request_hash) = 32", name="ck_scheduled_task_requests_hash"),
    )


def downgrade() -> None:
    """Remove only an unused receipt table; keep retry protection after first use."""
    connection = op.get_bind()
    if connection.execute(sa.text("SELECT 1 FROM scheduled_task_requests LIMIT 1")).first():
        raise RuntimeError("Cannot drop scheduled-task request receipts after mutations used them")
    op.drop_table("scheduled_task_requests")
