"""add session_pull_requests table

Revision ID: pr1a2b3c4d5e
Revises: c8e4f2a6b901
Create Date: 2026-10-22 00:00:00.000000

Adds ``session_pull_requests``: the pull requests linked to a session and the
last state its host fetched, so the Pull Requests tab lists them while the
host is offline. One row per ``(workspace_id, conversation_id, url_cksum)``,
where ``url_cksum`` is sha256 of the PR URL. New table only, created at the
current schema state with the tenant ``workspace_id`` leading the primary key
and no foreign keys (schema Rule R032).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY as MySQLBinary

from omnigent.db.db_models import Uuid16

revision: str = "pr1a2b3c4d5e"
down_revision: str | None = "c8e4f2a6b901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the ``session_pull_requests`` table."""
    op.create_table(
        "session_pull_requests",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        # sha256(url): BINARY(32) on MySQL, where a BLOB is not indexable.
        sa.Column(
            "url_cksum",
            sa.LargeBinary(32).with_variant(MySQLBinary(32), "mysql"),
            nullable=False,
        ),
        sa.Column("url", sa.String(2048), nullable=False),
        sa.Column("provider", sa.String(64), nullable=False),
        sa.Column("host", sa.String(255), nullable=False),
        sa.Column("repository", sa.String(512), nullable=False),
        sa.Column("number", sa.BigInteger(), nullable=False),
        # Enum stored as a stable int code (see omnigent.db.enum_codecs
        # SESSION_PR_RELATIONSHIP: inferred=1, worked_on=2, attached=3, created=4).
        sa.Column("relationship", sa.SmallInteger(), nullable=False),
        sa.Column("first_seen_at", sa.BigInteger(), nullable=False),
        sa.Column("last_seen_at", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(1024), nullable=True),
        # Enum stored as a stable int code (see omnigent.db.enum_codecs
        # SESSION_PR_STATE: OPEN=1, CLOSED=2, MERGED=3).
        sa.Column("state", sa.SmallInteger(), nullable=True),
        sa.Column("is_draft", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("state_checked_at", sa.BigInteger(), nullable=True),
        sa.Column("removed_at", sa.BigInteger(), nullable=True),
        sa.CheckConstraint(
            "relationship IN (1, 2, 3, 4)", name="ck_session_pull_requests_relationship"
        ),
        sa.CheckConstraint("state IN (1, 2, 3)", name="ck_session_pull_requests_state"),
        sa.PrimaryKeyConstraint("workspace_id", "conversation_id", "url_cksum"),
    )


def downgrade() -> None:
    """Drop the ``session_pull_requests`` table."""
    op.drop_table("session_pull_requests")
