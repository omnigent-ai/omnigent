"""Add the user_credentials table.

Revision ID: z9a2b3c4d5e6
Revises: d1e2f3a4b5c6
Create Date: 2026-07-20 00:00:00.000000

One row per (workspace, user, provider): an external-service credential
connected in the web UI (Settings → Credentials), e.g. a GitHub OAuth
token. ``token_encrypted`` holds Fernet ciphertext — the encryption key
lives only in the ``OMNIGENT_CREDENTIAL_ENCRYPTION_KEY`` env var, so the
table alone never yields a usable token. Read at managed-sandbox launch
to inject the owner's ``GIT_TOKEN``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "z9a2b3c4d5e6"
down_revision: str | None = "d1e2f3a4b5c6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_credentials",
        sa.Column(
            "workspace_id",
            sa.BigInteger,
            primary_key=True,
            nullable=False,
            server_default="0",
        ),
        sa.Column("user_id", sa.String(256), primary_key=True, nullable=False),
        sa.Column("provider", sa.String(32), primary_key=True, nullable=False),
        sa.Column("token_encrypted", sa.Text, nullable=False),
        sa.Column("login", sa.String(256), nullable=False),
        sa.Column("scopes", sa.String(256), nullable=False, server_default=""),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Integer, nullable=False),
    )


def downgrade() -> None:
    op.drop_table("user_credentials")
