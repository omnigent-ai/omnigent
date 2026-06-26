"""add mcp_servers table

Revision ID: p1a2b3c4d5e6
Revises: n1a2b3c4d5e6
Create Date: 2026-06-26 00:00:00.000000

Creates the ``mcp_servers`` table backing standalone, owner-scoped MCP
server configs. These are reusable connections a user registers once
and references when creating agents (and can verify — connect + list
tools — before saving).

Secret-bearing ``headers`` and ``env`` are stored JSON-encoded
alongside ``args``; the API never returns their values, only their keys.
Uniqueness is per-owner (``ix_mcp_servers_owner_name``) so two users may
each register a server under the same name.

NOTE: this revision chains off ``n1a2b3c4d5e6``. If the standalone-agents
migration (``o1a2b3c4d5e6``) is also present in a deployment, re-point one
of the two ``down_revision``s so the chain stays linear (single head).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "p1a2b3c4d5e6"
down_revision: str | None = "n1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=True),
        sa.Column("owner", sa.String(length=256), nullable=True),
        sa.Column("name", sa.String(length=256), nullable=False),
        sa.Column("transport", sa.String(length=16), nullable=False),
        sa.Column("url", sa.String(length=2048), nullable=True),
        sa.Column("headers_json", sa.Text(), nullable=True),
        sa.Column("command", sa.String(length=1024), nullable=True),
        sa.Column("args_json", sa.Text(), nullable=True),
        sa.Column("env_json", sa.Text(), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mcp_servers_created_at", "mcp_servers", ["created_at"])
    op.create_index("ix_mcp_servers_owner", "mcp_servers", ["owner"])
    op.create_index(
        "ix_mcp_servers_owner_name",
        "mcp_servers",
        ["owner", "name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_mcp_servers_owner_name", table_name="mcp_servers")
    op.drop_index("ix_mcp_servers_owner", table_name="mcp_servers")
    op.drop_index("ix_mcp_servers_created_at", table_name="mcp_servers")
    op.drop_table("mcp_servers")
