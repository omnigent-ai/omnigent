"""Add saved inference configuration to Omnigent conversation metadata.

Revision ID: hi1b2c3d4e5f
Revises: hh1b2c3d4e5f
Create Date: 2026-09-18 00:00:00.000000

Existing conversations retain NULL and their existing provider behavior.
MySQL uses LONGTEXT because unrestricted catalogs can exceed TEXT's 64 KiB limit.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import LONGTEXT

revision: str = "hi1b2c3d4e5f"
down_revision: str | None = "hh1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.add_column(
            sa.Column(
                "inference_snapshot",
                sa.Text().with_variant(LONGTEXT(), "mysql"),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("omnigent_conversation_metadata") as batch_op:
        batch_op.drop_column("inference_snapshot")
