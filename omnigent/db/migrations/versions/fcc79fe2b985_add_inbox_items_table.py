"""add inbox_items table

Revision ID: fcc79fe2b985
Revises: a7d4c2e9f610
Create Date: 2026-11-09 00:00:00.000000

Adds the ``inbox_items`` table: suggestions an agent posts to its owner's
Inbox. What the agent posted and how the owner answered are storage envelopes
in bounded binary columns, so a deployment can encrypt them later without a
schema change; the other columns route, order, and deduplicate.
``workspace_id`` leads the primary key, there are no foreign keys (schema Rule
R032), and no column has a SQL default: the application supplies every value.
The table is new and unused until the Inbox item routes ship, so deploying it
first, and rolling it back by downgrade before those routes ship, is safe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "fcc79fe2b985"
down_revision: str | None = "a7d4c2e9f610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SQLITE_FAMILY = frozenset({"sqlite", "cloudflare_d1"})
# SQLite enforces neither declared lengths nor types; these bound what it stores.
_SQLITE_CHECKS: tuple[tuple[str, str], ...] = (
    ("user_id IS NULL OR length(CAST(user_id AS BLOB)) <= 512", "ck_inbox_items_user_id_bytes"),
    (
        "prompt_version IS NULL OR length(CAST(prompt_version AS BLOB)) <= 256",
        "ck_inbox_items_prompt_version_bytes",
    ),
    (
        "dedupe_key IS NULL OR length(CAST(dedupe_key AS BLOB)) <= 512",
        "ck_inbox_items_dedupe_key_bytes",
    ),
    ("content IS NULL OR length(CAST(content AS BLOB)) <= 16383", "ck_inbox_items_content_bytes"),
    (
        "resolution IS NULL OR length(CAST(resolution AS BLOB)) <= 16383",
        "ck_inbox_items_resolution_bytes",
    ),
    (
        "workspace_id IS NULL OR typeof(workspace_id) = 'integer'",
        "ck_inbox_items_workspace_id_integer",
    ),
    ("created_at IS NULL OR typeof(created_at) = 'integer'", "ck_inbox_items_created_at_integer"),
    (
        "resolved_at IS NULL OR typeof(resolved_at) = 'integer'",
        "ck_inbox_items_resolved_at_integer",
    ),
    ("expires_at IS NULL OR typeof(expires_at) = 'integer'", "ck_inbox_items_expires_at_integer"),
    ("length(CAST(id AS BLOB)) = 16", "ck_inbox_items_id_uuid"),
    ("length(CAST(source_session_id AS BLOB)) = 16", "ck_inbox_items_source_session_id_uuid"),
    ("length(CAST(agent_id AS BLOB)) = 16", "ck_inbox_items_agent_id_uuid"),
)


def _sqlite_checks() -> list[sa.CheckConstraint]:
    """The SQLite-only CHECKs, or none on databases that enforce column types."""
    if op.get_bind().dialect.name not in _SQLITE_FAMILY:
        return []
    return [sa.CheckConstraint(expr, name=name) for expr, name in _SQLITE_CHECKS]


def upgrade() -> None:
    """Create the ``inbox_items`` table and its indexes."""
    op.create_table(
        "inbox_items",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("user_id", sa.String(128), nullable=False),
        sa.Column("source_session_id", Uuid16(), nullable=False),
        sa.Column("agent_id", Uuid16(), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=True),
        sa.Column("dedupe_key", sa.String(128), nullable=False),
        # INBOX_ITEM_STATE: unread=1, read=2, done=3, dismissed=4.
        sa.Column("state", sa.SmallInteger(), nullable=False),
        # Storage envelopes, capped at 16,383 bytes by the model: what the agent
        # posted, and how the owner answered (NULL until they do).
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("resolution", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("resolved_at", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("state IN (1, 2, 3, 4)", name="ck_inbox_items_state"),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        *_sqlite_checks(),
    )
    op.create_index(
        "ix_inbox_items_user_created",
        "inbox_items",
        ["workspace_id", "user_id", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_inbox_items_user_state_created",
        "inbox_items",
        ["workspace_id", "user_id", "state", "created_at", "id"],
        unique=False,
    )
    op.create_index(
        "ix_inbox_items_session",
        "inbox_items",
        ["workspace_id", "user_id", "source_session_id"],
        unique=False,
    )
    op.create_index(
        "uq_inbox_items_user_dedupe_key",
        "inbox_items",
        ["workspace_id", "user_id", "dedupe_key"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the ``inbox_items`` table."""
    op.drop_index("uq_inbox_items_user_dedupe_key", table_name="inbox_items")
    op.drop_index("ix_inbox_items_session", table_name="inbox_items")
    op.drop_index("ix_inbox_items_user_state_created", table_name="inbox_items")
    op.drop_index("ix_inbox_items_user_created", table_name="inbox_items")
    op.drop_table("inbox_items")
