"""Add Company Brain installation, connection, selection, and sync-run tables.

Revision ID: cb1a2b3c4d5e
Revises: ga1b2c3d4e5f
Create Date: 2026-08-26 00:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "cb1a2b3c4d5e"
down_revision: str | None = "ga1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oauth_state_nonces",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("nonce_sha256", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "nonce_sha256"),
    )
    op.create_index(
        "ix_oauth_state_nonces_expiry",
        "oauth_state_nonces",
        ["workspace_id", "expires_at"],
    )
    op.create_table(
        "brain_installations",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("repo_path", sa.String(2048), nullable=False),
        sa.Column("repo_url", sa.String(2048), nullable=True),
        sa.Column("gbrain_state_path", sa.String(2048), nullable=False),
        sa.Column("mcp_url", sa.String(2048), nullable=True),
        sa.Column("mcp_auth_ref", sa.String(512), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="ready"),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "status IN ('provisioning', 'ready', 'degraded', 'disabled')",
            name="ck_brain_installations_status",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint("workspace_id", name="uq_brain_installations_workspace"),
    )
    op.create_table(
        "integration_connections",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("credential_ciphertext", sa.Text(), nullable=True),
        sa.Column("account_label", sa.String(512), nullable=True),
        sa.Column("granted_scopes_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="connected"),
        sa.Column("last_error", sa.String(512), nullable=True),
        sa.Column("created_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "provider IN ('google', 'slack', 'notion')",
            name="ck_integration_connections_provider",
        ),
        sa.CheckConstraint(
            "status IN ('connected', 'needs_reconnect', 'disconnected', 'error')",
            name="ck_integration_connections_status",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_integration_connections_provider",
        "integration_connections",
        ["workspace_id", "provider", "created_at", "id"],
    )
    op.create_table(
        "integration_selections",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("connection_id", Uuid16(), nullable=False),
        sa.Column("external_resource_id", sa.String(512), nullable=False),
        sa.Column("resource_name", sa.String(512), nullable=False),
        sa.Column("resource_type", sa.String(64), nullable=False),
        sa.Column("source_url", sa.String(2048), nullable=True),
        sa.Column("transform_profile", sa.String(128), nullable=False),
        sa.Column("visibility_class", sa.String(32), nullable=False, server_default="org-shared"),
        sa.Column("rrule", sa.String(512), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("state", sa.String(32), nullable=False, server_default="active"),
        sa.Column("last_synced_at", sa.Integer(), nullable=True),
        sa.Column("page_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.String(512), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "visibility_class = 'org-shared'",
            name="ck_integration_selections_visibility",
        ),
        sa.CheckConstraint(
            "state IN ('active', 'paused', 'disconnected')",
            name="ck_integration_selections_state",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "connection_id",
            "external_resource_id",
            name="uq_integration_selections_resource",
        ),
    )
    op.create_index(
        "ix_integration_selections_connection",
        "integration_selections",
        ["workspace_id", "connection_id", "created_at", "id"],
    )
    op.create_table(
        "integration_sync_runs",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("connection_id", Uuid16(), nullable=False),
        sa.Column("selection_id", Uuid16(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("trigger_kind", sa.String(32), nullable=False),
        sa.Column("fetched_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("changed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("deleted_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("skipped_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("commit_sha", sa.String(64), nullable=True),
        sa.Column("gbrain_result_json", sa.LargeBinary(), nullable=True),
        sa.Column("error", sa.String(512), nullable=True),
        sa.Column("scheduled_at", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.Integer(), nullable=True),
        sa.Column("finished_at", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'skipped')",
            name="ck_integration_sync_runs_status",
        ),
        sa.CheckConstraint(
            "trigger_kind IN ('manual', 'schedule', 'retry')",
            name="ck_integration_sync_runs_trigger",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_integration_sync_runs_selection",
        "integration_sync_runs",
        ["workspace_id", "selection_id", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_integration_sync_runs_selection", table_name="integration_sync_runs")
    op.drop_table("integration_sync_runs")
    op.drop_index("ix_integration_selections_connection", table_name="integration_selections")
    op.drop_table("integration_selections")
    op.drop_index("ix_integration_connections_provider", table_name="integration_connections")
    op.drop_table("integration_connections")
    op.drop_table("brain_installations")
    op.drop_index("ix_oauth_state_nonces_expiry", table_name="oauth_state_nonces")
    op.drop_table("oauth_state_nonces")
