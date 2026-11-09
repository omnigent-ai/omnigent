"""add inbox_items table

Revision ID: fcc79fe2b985
Revises: f1c7a9e2b406
Create Date: 2026-11-09 00:00:00.000000

Adds the ``inbox_items`` table: suggestions an agent posts to its owner's
Inbox. ``workspace_id`` leads the primary key, there are no foreign keys
(schema Rule R032), and no column has a SQL default: the application supplies
every value. The table is new and unused until the Inbox item routes ship, so
deploying it first, and rolling it back by downgrade before those routes ship,
is safe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "fcc79fe2b985"
down_revision: str | None = "f1c7a9e2b406"
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
    ("title IS NULL OR length(CAST(title AS BLOB)) <= 1024", "ck_inbox_items_title_bytes"),
    (
        "primary_link IS NULL OR length(CAST(primary_link AS BLOB)) <= 8192",
        "ck_inbox_items_primary_link_bytes",
    ),
    ("topic IS NULL OR length(CAST(topic AS BLOB)) <= 512", "ck_inbox_items_topic_bytes"),
    (
        "dedupe_key IS NULL OR length(CAST(dedupe_key AS BLOB)) <= 512",
        "ck_inbox_items_dedupe_key_bytes",
    ),
    ("body_md IS NULL OR length(CAST(body_md AS BLOB)) <= 16383", "ck_inbox_items_body_md_bytes"),
    ("links IS NULL OR length(CAST(links AS BLOB)) <= 16383", "ck_inbox_items_links_bytes"),
    ("actions IS NULL OR length(CAST(actions AS BLOB)) <= 16383", "ck_inbox_items_actions_bytes"),
    ("chart IS NULL OR length(CAST(chart AS BLOB)) <= 16383", "ck_inbox_items_chart_bytes"),
    (
        "feedback_text IS NULL OR length(CAST(feedback_text AS BLOB)) <= 16383",
        "ck_inbox_items_feedback_text_bytes",
    ),
    (
        "workspace_id IS NULL OR typeof(workspace_id) = 'integer'",
        "ck_inbox_items_workspace_id_integer",
    ),
    ("created_at IS NULL OR typeof(created_at) = 'integer'", "ck_inbox_items_created_at_integer"),
    ("read_at IS NULL OR typeof(read_at) = 'integer'", "ck_inbox_items_read_at_integer"),
    ("acted_at IS NULL OR typeof(acted_at) = 'integer'", "ck_inbox_items_acted_at_integer"),
    ("expires_at IS NULL OR typeof(expires_at) = 'integer'", "ck_inbox_items_expires_at_integer"),
    (
        "resolution_action IS NULL OR typeof(resolution_action) = 'integer'",
        "ck_inbox_items_resolution_action_integer",
    ),
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
        sa.Column("session_slot", sa.SmallInteger(), nullable=False),
        sa.Column("agent_id", Uuid16(), nullable=False),
        sa.Column("prompt_version", sa.String(64), nullable=True),
        # INBOX_ITEM_KIND: followup=1, info=2, reminder=3.
        sa.Column("kind", sa.SmallInteger(), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        # Compressed text, capped at 16,383 stored bytes by the model.
        sa.Column("body_md", sa.LargeBinary(), nullable=False),
        sa.Column("links", sa.LargeBinary(), nullable=False),
        sa.Column("actions", sa.LargeBinary(), nullable=False),
        sa.Column("chart", sa.LargeBinary(), nullable=True),
        sa.Column("primary_link", sa.String(2048), nullable=True),
        sa.Column("topic", sa.String(128), nullable=False),
        sa.Column("dedupe_key", sa.String(128), nullable=False),
        # INBOX_ITEM_STATE: unread=1, read=2, done=3, dismissed=4.
        sa.Column("state", sa.SmallInteger(), nullable=False),
        # INBOX_ITEM_RESOLUTION: none=0, action_taken=1, already_done=2, useful=3,
        # not_relevant=4, incorrect=5, not_now=6.
        sa.Column("resolution", sa.SmallInteger(), nullable=False),
        sa.Column("resolution_action", sa.SmallInteger(), nullable=True),
        sa.Column("feedback_text", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("read_at", sa.BigInteger(), nullable=True),
        sa.Column("acted_at", sa.BigInteger(), nullable=True),
        sa.Column("expires_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint("kind IN (1, 2, 3)", name="ck_inbox_items_kind"),
        sa.CheckConstraint("state IN (1, 2, 3, 4)", name="ck_inbox_items_state"),
        sa.CheckConstraint(
            "resolution IN (0, 1, 2, 3, 4, 5, 6)", name="ck_inbox_items_resolution"
        ),
        sa.CheckConstraint(
            "session_slot IN (0, 1, 2, 3, 4, 5, 6, 7, 8, 9)", name="ck_inbox_items_session_slot"
        ),
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
        "uq_inbox_items_user_dedupe_key",
        "inbox_items",
        ["workspace_id", "user_id", "dedupe_key"],
        unique=True,
    )
    op.create_index(
        "uq_inbox_items_session_slot",
        "inbox_items",
        ["workspace_id", "user_id", "source_session_id", "session_slot"],
        unique=True,
    )


def downgrade() -> None:
    """Drop the ``inbox_items`` table."""
    op.drop_index("uq_inbox_items_session_slot", table_name="inbox_items")
    op.drop_index("uq_inbox_items_user_dedupe_key", table_name="inbox_items")
    op.drop_index("ix_inbox_items_user_state_created", table_name="inbox_items")
    op.drop_index("ix_inbox_items_user_created", table_name="inbox_items")
    op.drop_table("inbox_items")
