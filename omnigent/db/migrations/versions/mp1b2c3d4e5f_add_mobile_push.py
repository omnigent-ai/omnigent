"""Add workspace-scoped FCM registrations and a leased notification outbox.

The server automatically migrates at startup; deploy schema before feature code.
The tables are inert for older code, but old replicas refuse a newer schema when
restarting. Prefer flag-off roll-forward; stop new replicas before downgrading:
OMNIGENT_DB_URL=… alembic -c omnigent/db/alembic.ini downgrade mm1a2b3c4d5e
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY

revision: str = "mp1b2c3d4e5f"
down_revision: str | None = "mm1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mobile_push_devices",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("installation_id", sa.String(128), primary_key=True),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("platform", sa.SmallInteger(), nullable=False),
        sa.Column("fcm_token", sa.String(1024), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column(
            "generation", sa.LargeBinary(16).with_variant(BINARY(16), "mysql"), nullable=False
        ),
        sa.Column("account_generation", sa.String(32), nullable=True),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("workspace_id", "token_hash", name="uq_mobile_push_device_token"),
        sa.CheckConstraint("platform IN (1, 2)", name="ck_mobile_push_platform"),
    )
    op.create_index(
        "ix_mobile_push_devices_user",
        "mobile_push_devices",
        ["workspace_id", "user_id", "expires_at", "installation_id"],
    )
    op.create_table(
        "mobile_push_outbox",
        sa.Column("workspace_id", sa.BigInteger(), primary_key=True),
        sa.Column("id", sa.LargeBinary(16).with_variant(BINARY(16), "mysql"), primary_key=True),
        sa.Column(
            "session_id", sa.LargeBinary(16).with_variant(BINARY(16), "mysql"), nullable=False
        ),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("installation_id", sa.String(128), nullable=False),
        sa.Column(
            "device_generation",
            sa.LargeBinary(16).with_variant(BINARY(16), "mysql"),
            nullable=False,
        ),
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("reason", sa.String(128), nullable=True),
        sa.Column("not_before", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("lease", sa.LargeBinary(16).with_variant(BINARY(16), "mysql"), nullable=True),
        sa.Column("lease_until", sa.BigInteger(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("delivered", sa.Boolean(), nullable=False),
        sa.UniqueConstraint(
            "workspace_id",
            "session_id",
            "user_id",
            "installation_id",
            "device_generation",
            "kind",
            name="uq_mobile_push_outbox_intent",
        ),
        sa.CheckConstraint("kind IN (1, 2, 3)", name="ck_mobile_push_kind"),
    )
    op.create_index(
        "ix_mobile_push_outbox_due",
        "mobile_push_outbox",
        ["workspace_id", "delivered", "not_before", "lease_until", "id"],
    )
    op.create_index(
        "ix_mobile_push_devices_expiry",
        "mobile_push_devices",
        ["expires_at", "workspace_id", "installation_id"],
    )
    op.create_index(
        "ix_mobile_push_outbox_expiry", "mobile_push_outbox", ["expires_at", "workspace_id", "id"]
    )
    op.create_index(
        "ix_mobile_push_outbox_device",
        "mobile_push_outbox",
        ["workspace_id", "installation_id", "id"],
    )
    op.create_index(
        "ix_mobile_push_outbox_user", "mobile_push_outbox", ["workspace_id", "user_id", "id"]
    )
    op.create_index(
        "ix_mobile_push_outbox_tenants",
        "mobile_push_outbox",
        ["delivered", "workspace_id", "id"],
    )
    op.create_index(
        "ix_mobile_push_outbox_activity",
        "mobile_push_outbox",
        ["workspace_id", "session_id", "kind", "delivered", "id"],
    )


def downgrade() -> None:
    op.drop_table("mobile_push_outbox")
    op.drop_table("mobile_push_devices")
