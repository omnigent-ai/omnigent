"""add integration_connections table

Revision ID: ga1b2c3d4e5f
Revises: d5e9f1a2b3c4
Create Date: 2026-07-15 00:00:00.000000

Adds the provider-agnostic ``integration_connections`` table backing every
per-user "Connect …" integration (GitHub App today; MCP connectors later) and
the credential broker that vends the secret to managed sandboxes on demand. See
``designs/CREDENTIAL_STORE.md``.

One row per ``(workspace_id, user_id, provider, account_id)``. ``secret_enc``
holds a Fernet-ciphertext JSON blob of all secret material (encrypted
server-side); plaintext never lands in the database. Non-secret metadata
(login, ids, scopes, expiries) is JSON in ``metadata_json``. New table only —
no existing table changes — so no batch rebuild is required.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ga1b2c3d4e5f"
# Chains off the current alembic head. Re-point on rebase whenever main advances
# its head, or a deploy-time `alembic upgrade head` hits multiple heads.
down_revision: str | None = "d5e9f1a2b3c4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the integration_connections table."""
    op.create_table(
        "integration_connections",
        sa.Column(
            "workspace_id", sa.BigInteger, primary_key=True, nullable=False, server_default="0"
        ),
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("provider", sa.String(64), primary_key=True),
        sa.Column(
            "account_id", sa.String(128), primary_key=True, nullable=False, server_default=""
        ),
        sa.Column("secret_enc", sa.Text, nullable=False),
        sa.Column("metadata_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Integer, nullable=False),
    )


def downgrade() -> None:
    """Drop the integration_connections table."""
    op.drop_table("integration_connections")
