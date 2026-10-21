"""Add workspace-scoped sandbox profiles and immutable session bindings.

Revision ID: nn1a2b3c4d5e
Revises: nf12ab34cd56
Create Date: 2026-10-12 00:00:00.000000

Deploy this additive schema before the profile API. Older servers ignore both
tables, so application rollback preserves accepted profiles and bindings.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "nn1a2b3c4d5e"
down_revision: str | None = "nf12ab34cd56"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sandbox_profiles",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("config_json", sa.LargeBinary(16383), nullable=False),
        sa.Column("revision", sa.String(64), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
    )
    op.create_index(
        "ix_sandbox_profiles_enabled", "sandbox_profiles", ["workspace_id", "enabled", "id"]
    )
    op.create_table(
        "sandbox_profile_bindings",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("session_id", Uuid16(), primary_key=True),
        sa.Column("host_id", Uuid16(), nullable=True),
        sa.Column("snapshot_json", sa.LargeBinary(16383), nullable=False),
    )
    op.create_index(
        "ix_sandbox_profile_bindings_host",
        "sandbox_profile_bindings",
        ["workspace_id", "host_id", "session_id"],
    )


def downgrade() -> None:
    op.drop_table("sandbox_profile_bindings")
    op.drop_table("sandbox_profiles")
