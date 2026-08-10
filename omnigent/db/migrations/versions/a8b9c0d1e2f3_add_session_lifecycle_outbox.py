"""add session_lifecycle_outbox, session_lifecycle_cursors, session_elicitations

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
Create Date: 2026-08-10 00:00:00.000000

Adds the durable-session-lifecycle-push tables (OMN-104): a transactional
outbox of stable-ID lifecycle events pushed to the configured manager
webhook (``session_lifecycle_outbox``), its per-session sequence allocator
(``session_lifecycle_cursors``), and a durable elicitation ledger
(``session_elicitations``) recording human-decision verdicts so they survive
a server restart. See
``docs/architecture/2026-08-10-durable-session-lifecycle-push.md``.

All three tables are brand-new, carry ``workspace_id`` as the leading
primary-key member (matching every other table), and have no foreign-key
constraints (schema Rule R032) — the ``session_id`` relationship is
application-owned, like ``scheduled_task_runs.conversation_id``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "a8b9c0d1e2f3"
down_revision: str | None = "f7a8b9c0d1e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the outbox, cursor, and elicitation-ledger tables."""
    op.create_table(
        "session_lifecycle_cursors",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("session_id", Uuid16(), nullable=False),
        sa.Column("next_sequence", sa.BigInteger(), nullable=False, server_default="1"),
        sa.PrimaryKeyConstraint("workspace_id", "session_id"),
    )

    op.create_table(
        "session_lifecycle_outbox",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        # Externally stable event_id (deterministic UUIDv5), stored as 16 raw
        # bytes (Uuid16).
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("session_id", Uuid16(), nullable=False),
        # Stable int code (see omnigent.db.enum_codecs
        # SESSION_LIFECYCLE_EVENT_TYPE: session.completed=1, session.failed=2,
        # session.awaiting_decision=3, session.resumed=4).
        sa.Column("event_type", sa.SmallInteger(), nullable=False),
        sa.Column("transition_key", sa.String(192), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_version", sa.SmallInteger(), nullable=False, server_default="1"),
        # Redacted/allowlisted JSON event payload, stored compressed.
        sa.Column("payload", sa.LargeBinary(), nullable=False),
        # Stable int code (see omnigent.db.enum_codecs
        # SESSION_LIFECYCLE_OUTBOX_STATUS: pending=1, leased=2, delivered=3,
        # dead_letter=4, paused=5).
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.Integer(), nullable=True),
        sa.Column("last_attempt_at", sa.Integer(), nullable=True),
        sa.Column("delivered_at", sa.Integer(), nullable=True),
        sa.Column("last_http_status", sa.SmallInteger(), nullable=True),
        sa.Column("last_error_code", sa.String(64), nullable=True),
        sa.Column("last_error_message", sa.String(256), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "event_type IN (1, 2, 3, 4)",
            name="ck_session_lifecycle_outbox_event_type",
        ),
        sa.CheckConstraint(
            "status IN (1, 2, 3, 4, 5)",
            name="ck_session_lifecycle_outbox_status",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "uq_session_lifecycle_outbox_session_sequence",
        "session_lifecycle_outbox",
        ["workspace_id", "session_id", "sequence"],
        unique=True,
    )
    op.create_index(
        "uq_session_lifecycle_outbox_transition",
        "session_lifecycle_outbox",
        ["workspace_id", "session_id", "event_type", "transition_key"],
        unique=True,
    )
    op.create_index(
        "ix_session_lifecycle_outbox_claim",
        "session_lifecycle_outbox",
        ["status", "next_attempt_at", "workspace_id"],
        unique=False,
    )
    op.create_index(
        "ix_session_lifecycle_outbox_session_order",
        "session_lifecycle_outbox",
        ["workspace_id", "session_id", "sequence", "status"],
        unique=False,
    )

    op.create_table(
        "session_elicitations",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        # == elicitation_id, e.g. "elicit_a1b2c3..." — a prefixed opaque
        # token, not a bare-hex UUID, so this is a plain String column,
        # not Uuid16 (unlike every other id column in this migration).
        sa.Column("id", sa.String(128), nullable=False),
        sa.Column("session_id", Uuid16(), nullable=False),
        # Stable int code (see omnigent.db.enum_codecs
        # SESSION_ELICITATION_STATUS: pending=1, decided=2,
        # delivered_to_runner=3, expired=4).
        sa.Column("status", sa.SmallInteger(), nullable=False, server_default="1"),
        # Allowlisted request/decision fields, stored compressed.
        sa.Column("request_payload", sa.LargeBinary(), nullable=False),
        sa.Column("decision_payload", sa.LargeBinary(), nullable=True),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("decided_at", sa.Integer(), nullable=True),
        sa.Column("resolved_at", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "status IN (1, 2, 3, 4)",
            name="ck_session_elicitations_status",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
    )
    op.create_index(
        "ix_session_elicitations_session_id",
        "session_elicitations",
        ["workspace_id", "session_id"],
        unique=False,
    )


def downgrade() -> None:
    """Drop the elicitation-ledger, outbox, and cursor tables."""
    op.drop_index("ix_session_elicitations_session_id", table_name="session_elicitations")
    op.drop_table("session_elicitations")

    op.drop_index(
        "ix_session_lifecycle_outbox_session_order", table_name="session_lifecycle_outbox"
    )
    op.drop_index("ix_session_lifecycle_outbox_claim", table_name="session_lifecycle_outbox")
    op.drop_index("uq_session_lifecycle_outbox_transition", table_name="session_lifecycle_outbox")
    op.drop_index(
        "uq_session_lifecycle_outbox_session_sequence", table_name="session_lifecycle_outbox"
    )
    op.drop_table("session_lifecycle_outbox")

    op.drop_table("session_lifecycle_cursors")
