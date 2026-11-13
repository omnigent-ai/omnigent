"""add inbox_items table

Revision ID: fcc79fe2b985
Revises: a7d4c2e9f610
Create Date: 2026-11-09 00:00:00.000000

Adds the ``inbox_items`` table: suggestions an agent posts to its owner's
Inbox. What the agent posted and how the owner answered are storage envelopes
(compressed JSON behind a two-byte header that names it as plaintext or
ciphertext) in bounded binary columns, so a deployment can encrypt them later
without a schema change; the other columns route, order, and deduplicate.
``workspace_id`` leads the primary key, there are no foreign keys (schema Rule
R032), and no column has a SQL default: the application supplies every value.
The table is new and unused until the Inbox item routes ship, so deploying it
first, and rolling it back by downgrade before those routes ship, is safe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARBINARY

from omnigent.db.db_models import Uuid16

revision: str = "fcc79fe2b985"
down_revision: str | None = "a8be64286ffc"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Largest stored envelope, per the database guide's per-value limit.
_ENVELOPE_BYTES = 16_383
# A BLOB on PostgreSQL and SQLite, whose binary types have no length; a
# VARBINARY of exactly the cap on MySQL. The application also checks the
# cap on every write.
_ENVELOPE = sa.LargeBinary().with_variant(VARBINARY(_ENVELOPE_BYTES), "mysql")

_SQLITE_FAMILY = frozenset({"sqlite", "cloudflare_d1"})
# SQLite enforces neither declared lengths nor types; these bound what it stores.
_SQLITE_CHECKS: tuple[tuple[str, str], ...] = (
    ("user_id IS NULL OR length(CAST(user_id AS BLOB)) <= 512", "ck_inbox_items_user_id_bytes"),
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
        # Tenant partition key (0 = default); leads the primary key.
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("id", Uuid16(), nullable=False),
        # The owner whose Inbox the item is in.
        sa.Column("user_id", sa.String(128), nullable=False),
        # The session whose agent posted the item, and that agent.
        sa.Column("source_session_id", Uuid16(), nullable=False),
        sa.Column("agent_id", Uuid16(), nullable=False),
        # Versioned duplicate key, "v1:" + a hash of the kind, primary link, and
        # topic; unique per owner.
        sa.Column("dedupe_key", sa.String(128), nullable=False),
        # INBOX_ITEM_STATUS: unread=1, read=2, done=3, dismissed=4.
        sa.Column("status", sa.SmallInteger(), nullable=False),
        # What the agent posted: kind, title, body, links, actions, topic,
        # primary link, and media blocks, as one envelope.
        sa.Column("content", _ENVELOPE, nullable=False),
        # How the owner answered: reason, action index, and comment, as one
        # envelope. NULL until they do.
        sa.Column("resolution", _ENVELOPE, nullable=True),
        # Unix epoch seconds: when posted, when last resolved, and when a new
        # post with the same duplicate key may create a fresh item.
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("resolved_at", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("status IN (1, 2, 3, 4)", name="ck_inbox_items_status"),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        *_sqlite_checks(),
    )
    # Newest-first list of all of an owner's items.
    op.create_index(
        "ix_inbox_items_user_created",
        "inbox_items",
        ["workspace_id", "user_id", "created_at", "id"],
        unique=False,
    )
    # Newest-first list and bounded count of an owner's items in one status.
    op.create_index(
        "ix_inbox_items_user_status_created",
        "inbox_items",
        ["workspace_id", "user_id", "status", "created_at", "id"],
        unique=False,
    )
    # Bounded count of the items one session posted, for the per-session cap.
    op.create_index(
        "ix_inbox_items_session",
        "inbox_items",
        ["workspace_id", "user_id", "source_session_id", "id"],
        unique=False,
    )
    # One item per duplicate key per owner.
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
    op.drop_index("ix_inbox_items_user_status_created", table_name="inbox_items")
    op.drop_index("ix_inbox_items_user_created", table_name="inbox_items")
    op.drop_table("inbox_items")
