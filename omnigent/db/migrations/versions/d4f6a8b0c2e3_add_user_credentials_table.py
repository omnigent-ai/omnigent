"""add user_credentials table

Revision ID: d4f6a8b0c2e3
Revises: c3e5f7a9b1d2
Create Date: 2026-06-29 13:30:00.000000

Adds the ``user_credentials`` table: the per-user secret vault (#5). One
encrypted secret per (user, name); ``secret_encrypted`` holds a Fernet token,
never plaintext. UNIQUE(user_id, name); indexed on user_id for per-user lookup.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d4f6a8b0c2e3"
down_revision: str | None = "c3e5f7a9b1d2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_credentials",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=256), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("secret_encrypted", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_user_credentials_user_name"),
    )
    op.create_index(
        "ix_user_credentials_user_id", "user_credentials", ["user_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_user_credentials_user_id", table_name="user_credentials")
    op.drop_table("user_credentials")
