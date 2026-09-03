"""add connections table

Revision ID: ga1b2c3d4e5f
Revises: e5d9bc8ac650
Create Date: 2026-07-15 00:00:00.000000

Adds the provider-agnostic ``connections`` table backing every
per-user "Connect …" integration (GitHub App today; MCP connectors later) and
the credential broker that vends the secret to managed sandboxes on demand. See
``designs/CREDENTIAL_STORE.md``.

One row per ``(workspace_id, user_id, provider, account_id)``. ``secret_enc``
holds an AWS KMS ciphertext (base64) of the secret-material JSON blob (encrypted
server-side, bound to the row identity as the KMS encryption context); plaintext
never lands in the database. Non-secret metadata (login, ids, scopes, expiries)
is JSON in ``metadata_json``. New table only — no existing table changes — so no
batch rebuild is required.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "ga1b2c3d4e5f"
# Chains off the current alembic head. The branch is rebased onto upstream main,
# so this revision is a real ancestor here — a single head on the branch *and*
# on the PR merge (see tests/db/test_migration_connections.py, which
# guards against a second head). Re-point if main lands a newer migration before
# this merges.
down_revision: str | None = "e5d9bc8ac650"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the connections table."""
    op.create_table(
        "connections",
        sa.Column(
            "workspace_id", sa.BigInteger, primary_key=True, nullable=False, server_default="0"
        ),
        sa.Column("user_id", sa.String(128), primary_key=True),
        sa.Column("provider", sa.String(64), primary_key=True),
        sa.Column(
            "account_id", sa.String(128), primary_key=True, nullable=False, server_default=""
        ),
        sa.Column("secret_enc", sa.Text, nullable=False),
        sa.Column("metadata_json", sa.Text, nullable=False),
        sa.Column("created_at", sa.Integer, nullable=False),
        sa.Column("updated_at", sa.Integer, nullable=False),
    )


def downgrade() -> None:
    """Drop the connections table."""
    op.drop_table("connections")
