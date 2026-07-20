"""Add device_grants table for the OAuth device authorization grant.

Revision ID: d1e2f3a4b5c6
Revises: d7f1a2b3c4e5
Create Date: 2026-07-15

Backs the generic device-authorization grant (RFC 8628) — not tied to any
one client. One row per device-authorization request; secrets (device_code,
refresh token) are stored only as HMAC-SHA256 digests. See
omnigent/server/device_grant_store.py.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "d1e2f3a4b5c6"
down_revision: str | None = "d7f1a2b3c4e5"
branch_labels: tuple[str, ...] | None = None
depends_on: tuple[str, ...] | None = None


def upgrade() -> None:
    """Create the device_grants table and its lookup indexes."""
    op.create_table(
        "device_grants",
        sa.Column(
            "workspace_id",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("id", sa.String(128), nullable=False),
        sa.Column("device_code_hash", sa.String(64), nullable=False),
        sa.Column("user_code", sa.String(32), nullable=False),
        sa.Column("status", sa.SmallInteger(), nullable=False),
        sa.Column("client_id", sa.String(128), nullable=True),
        sa.Column("user_id", sa.String(128), nullable=True),
        sa.Column("refresh_token_hash", sa.String(64), nullable=True),
        sa.Column("prev_refresh_token_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("approved_at", sa.Integer(), nullable=True),
        sa.Column("last_polled_at", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.CheckConstraint("status IN (1, 2, 3, 4, 5)", name="ck_device_grants_status"),
    )
    op.create_index(
        "ix_device_grants_device_code_hash",
        "device_grants",
        ["workspace_id", "device_code_hash"],
    )
    op.create_index(
        "ix_device_grants_user_code",
        "device_grants",
        ["workspace_id", "user_code"],
    )
    op.create_index(
        "ix_device_grants_expires_at",
        "device_grants",
        ["workspace_id", "expires_at", "id"],
    )
    op.create_index(
        "ix_device_grants_refresh_hash",
        "device_grants",
        ["workspace_id", "refresh_token_hash"],
    )
    op.create_index(
        "ix_device_grants_prev_refresh_hash",
        "device_grants",
        ["workspace_id", "prev_refresh_token_hash"],
    )


def downgrade() -> None:
    """Drop the device_grants table and its indexes."""
    op.drop_index("ix_device_grants_prev_refresh_hash", table_name="device_grants")
    op.drop_index("ix_device_grants_refresh_hash", table_name="device_grants")
    op.drop_index("ix_device_grants_expires_at", table_name="device_grants")
    op.drop_index("ix_device_grants_user_code", table_name="device_grants")
    op.drop_index("ix_device_grants_device_code_hash", table_name="device_grants")
    op.drop_table("device_grants")
