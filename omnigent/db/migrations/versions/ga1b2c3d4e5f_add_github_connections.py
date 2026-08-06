"""add github_connections table

Revision ID: ga1b2c3d4e5f
Revises: e6f7a8b9c0d1
Create Date: 2026-07-15 00:00:00.000000

Adds the ``github_connections`` table backing the per-user GitHub App
integration — the web "Connect GitHub" flow and the per-user sandbox
authentication that vends the connecting user's GitHub token to managed
sandboxes on demand over the host<->server channel. See
``designs/GITHUB_APP_SANDBOX_AUTH.md``.

One row per ``(workspace_id, user_id)``. The token columns hold Fernet
ciphertext (encrypted server-side); plaintext tokens never land in the
database. New table only — no existing table changes — so no batch
rebuild is required.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ga1b2c3d4e5f"
down_revision: str | None = "e6f7a8b9c0d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the github_connections table."""
    op.create_table(
        "github_connections",
        sa.Column(
            "workspace_id", sa.BigInteger, primary_key=True, nullable=False, server_default="0"
        ),
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("github_login", sa.String(255), nullable=False),
        sa.Column("github_user_id", sa.BigInteger, nullable=False),
        sa.Column("access_token_enc", sa.Text, nullable=False),
        sa.Column("refresh_token_enc", sa.Text, nullable=True),
        sa.Column("token_expires_at", sa.Integer, nullable=True),
        sa.Column("refresh_token_expires_at", sa.Integer, nullable=True),
        sa.Column("scopes", sa.String(512), nullable=False, server_default=""),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Integer, nullable=False),
    )


def downgrade() -> None:
    """Drop the github_connections table."""
    op.drop_table("github_connections")
