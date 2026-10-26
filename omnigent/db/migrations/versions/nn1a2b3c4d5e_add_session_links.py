"""Persist directed session links, verified inputs, requests and replies.

Revision ID: nn1a2b3c4d5e
Revises: nf12ab34cd56

Additive metadata-database migration. Deploy before the disabled-by-default
session-links application feature. Older binaries ignore these tables. Disable
the feature before application rollback; retain tables and receipts. Downgrade
is for disposable test databases only: it deletes orchestration history.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import BINARY

revision = "nn1a2b3c4d5e"
down_revision = "nf12ab34cd56"
branch_labels = None
depends_on = None


def _uuid(name: str, *, primary_key: bool = False, nullable: bool = False) -> sa.Column:
    return sa.Column(
        name,
        sa.LargeBinary(16).with_variant(BINARY(16), "mysql"),
        primary_key=primary_key,
        nullable=nullable,
    )


def _workspace() -> sa.Column:
    return sa.Column("workspace_id", sa.BigInteger(), primary_key=True, nullable=False)


def _hash(name: str) -> sa.Column:
    return sa.Column(name, sa.LargeBinary(32).with_variant(BINARY(32), "mysql"), nullable=False)


def upgrade() -> None:
    op.create_table(
        "session_link_epochs",
        _workspace(),
        _uuid("session_id", primary_key=True),
        _uuid("generation"),
        sa.Column("blocked", sa.Boolean(), nullable=False),
    )
    op.create_table(
        "session_links",
        _workspace(),
        _uuid("source_id", primary_key=True),
        _uuid("target_id", primary_key=True),
        _uuid("generation"),
        _uuid("source_generation"),
        _uuid("target_generation"),
        sa.Column("approved_by", sa.String(128), nullable=False),
        _uuid("approver_generation", nullable=True),
        sa.Column("approved_at", sa.BigInteger(), nullable=False),
        sa.Column("revoked_at", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_session_links_target", "session_links", ["workspace_id", "target_id", "source_id"]
    )
    op.create_table(
        "session_input_authorities",
        _workspace(),
        _uuid("session_id", primary_key=True),
        _uuid("id", primary_key=True),
        sa.Column("requester", sa.String(128), nullable=False),
        _uuid("account_generation", nullable=True),
        sa.Column("runner_id", sa.String(64), nullable=False),
        _uuid("parent_request_id", nullable=True),
        sa.Column("captured_at", sa.BigInteger(), nullable=False),
        sa.Column("request_count", sa.SmallInteger(), nullable=False),
    )
    op.create_table(
        "linked_requests",
        _workspace(),
        _uuid("id", primary_key=True),
        _uuid("source_id"),
        _uuid("target_id"),
        _uuid("source_permission_id"),
        _uuid("target_permission_id"),
        _uuid("link_generation"),
        sa.Column("approved_by", sa.String(128), nullable=False),
        sa.Column("requester", sa.String(128), nullable=False),
        _uuid("account_generation", nullable=True),
        _uuid("input_id"),
        _uuid("parent_request_id", nullable=True),
        _uuid("root_request_id"),
        sa.Column("depth", sa.SmallInteger(), nullable=False),
        sa.Column("reply_count", sa.BigInteger(), nullable=False),
        _hash("operation_hash"),
        _hash("payload_hash"),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("expires_at", sa.BigInteger(), nullable=False),
        sa.Column("delivery_state", sa.SmallInteger(), nullable=False),
        sa.Column("accepted_runner_id", sa.String(64), nullable=True),
        sa.Column("accepted_at", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_linked_requests_operation",
        "linked_requests",
        ["workspace_id", "source_id", "operation_hash"],
        unique=True,
    )
    op.create_index(
        "ix_linked_requests_handoff",
        "linked_requests",
        [
            "workspace_id",
            "source_id",
            "target_id",
            "link_generation",
            "delivery_state",
            "expires_at",
            "id",
        ],
    )
    for scope, column in (
        ("source", "source_id"),
        ("target", "target_id"),
        ("source_acl", "source_permission_id"),
        ("target_acl", "target_permission_id"),
        ("requester", "requester"),
        ("approver", "approved_by"),
    ):
        op.create_index(
            f"ix_linked_requests_lease_{scope}",
            "linked_requests",
            ["workspace_id", column, "delivery_state", "expires_at", "id"],
        )
    op.create_table(
        "linked_replies",
        _workspace(),
        _uuid("request_id", primary_key=True),
        _uuid("id", primary_key=True),
        _hash("operation_hash"),
        _hash("payload_hash"),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.BigInteger(), nullable=False),
        sa.Column("published_at", sa.BigInteger(), nullable=True),
    )
    op.create_index(
        "ix_linked_replies_operation",
        "linked_replies",
        ["workspace_id", "request_id", "operation_hash"],
        unique=True,
    )
    op.create_index(
        "ix_linked_replies_sequence",
        "linked_replies",
        ["workspace_id", "request_id", "sequence"],
        unique=True,
    )


def downgrade() -> None:
    for table in (
        "linked_replies",
        "linked_requests",
        "session_input_authorities",
        "session_links",
        "session_link_epochs",
    ):
        op.drop_table(table)
