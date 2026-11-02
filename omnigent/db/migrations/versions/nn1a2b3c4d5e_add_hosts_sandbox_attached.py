"""Record externally owned, reusable sandbox attachments.

Revision ID: nn1a2b3c4d5e
Revises: mm1a2b3c4d5e
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "nn1a2b3c4d5e"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Preserve existing hosts' lifecycle ownership, including old inserts."""
    with op.batch_alter_table("hosts") as batch:
        batch.add_column(
            sa.Column("sandbox_attached", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    """Require attachments to be removed before discarding their protection."""
    hosts = sa.table("hosts", sa.column("sandbox_attached", sa.Boolean()))
    attached = (
        op.get_bind()
        .execute(
            sa.select(hosts.c.sandbox_attached).where(hosts.c.sandbox_attached.is_(True)).limit(1)
        )
        .first()
    )
    if attached is not None:
        raise RuntimeError("Detach externally owned sandbox hosts before downgrading")
    with op.batch_alter_table("hosts") as batch:
        batch.drop_column("sandbox_attached")
