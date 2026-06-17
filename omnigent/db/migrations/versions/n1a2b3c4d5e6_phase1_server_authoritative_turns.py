"""phase 1 server-authoritative turns

Revision ID: n1a2b3c4d5e6
Revises: m1a2b3c4d5e6
Create Date: 2026-06-17 00:00:00.000000

Phase 1 of the disconnect-tolerance work (issue #466, design
``designs/PHASE1_SERVER_AUTHORITATIVE_TURNS.md``). Introduces the
server-authoritative turn record so a turn's lifecycle is decoupled
from the client connection.

Adds:

- ``turns``: the durable, runner-owned turn record. ``id`` is the
  existing response/task id (``resp_...``). Carries the lifecycle
  ``status``, the failure ``error_code`` taxonomy, the dispatch
  payload, the runner lease (``lease_owner`` / ``lease_epoch`` /
  ``last_heartbeat_at`` / ``lease_expires_at``) and client-observation
  fields (``attached`` / ``last_client_seen``) that never gate
  execution. ``checkpoint_id`` is forward-compat for Phase 4 and
  always NULL here.

  Three indexes: ``ix_turns_live_lease`` (partial, the orphan-sweep
  hot path), ``ix_turns_conversation_created`` (per-session listing),
  and ``ux_turns_one_active_per_conversation`` (partial UNIQUE) which
  enforces queue-depth-1 per conversation so a racing dispatch hits an
  IntegrityError that the API converts to a structured 409
  Attach-Required. Partial predicates use the dialect-specific
  ``sqlite_where`` / ``postgresql_where`` kwargs, mirroring
  ``ix_conversations_parent_title_unique``.

- ``idempotency_keys``: client-generated UUIDv7 dedup so a retried
  send returns the original turn instead of creating a duplicate.

- ``conversations.default_send_intent``: optional per-session override
  for the send-intent default (NULL resolves from the system policy
  default).

All additions are nullable or carry a server_default, so the change is
online-safe (no table rewrite). The ``conversations`` column add uses
``batch_alter_table`` to stay SQLite-safe.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "n1a2b3c4d5e6"
down_revision: str | None = "m1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "turns",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), server_default="CREATED", nullable=False),
        sa.Column("error_code", sa.String(length=32), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("vendor", sa.String(length=32), nullable=False),
        sa.Column("intent", sa.String(length=16), nullable=False),
        sa.Column("input_json", sa.Text(), nullable=False),
        sa.Column("lease_owner", sa.String(length=64), nullable=True),
        sa.Column("lease_epoch", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_heartbeat_at", sa.Integer(), nullable=True),
        sa.Column("lease_expires_at", sa.Integer(), nullable=True),
        sa.Column("attached", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("last_client_seen", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("start_ts", sa.Integer(), nullable=True),
        sa.Column("end_ts", sa.Integer(), nullable=True),
        sa.Column("checkpoint_id", sa.String(length=64), nullable=True),
        sa.CheckConstraint(
            "status IN ('CREATED','QUEUED','RUNNING','PAUSING','PAUSED',"
            "'COMPLETED','FAILED','CANCELLED')",
            name="ck_turns_status",
        ),
        sa.CheckConstraint(
            "error_code IS NULL OR error_code IN "
            "('TRANSPORT_DISCONNECT','RUNNER_LOST','WORKER_BOOT_FAILURE',"
            "'WORKER_TASK_FAILURE','CANCELLED')",
            name="ck_turns_error_code",
        ),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    # Orphan-sweep hot path: live, leased turns by expiry.
    op.create_index(
        "ix_turns_live_lease",
        "turns",
        ["lease_expires_at"],
        unique=False,
        sqlite_where=sa.text("status IN ('RUNNING','PAUSING')"),
        postgresql_where=sa.text("status IN ('RUNNING','PAUSING')"),
    )
    # Per-session listing / current-turn lookup (newest first).
    op.create_index(
        "ix_turns_conversation_created",
        "turns",
        ["conversation_id", sa.text("created_at DESC")],
        unique=False,
    )
    # Queue-depth-1 invariant: at most one non-terminal turn per
    # conversation. A racing dispatch hits IntegrityError -> 409.
    op.create_index(
        "ux_turns_one_active_per_conversation",
        "turns",
        ["conversation_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('CREATED','QUEUED','RUNNING','PAUSING','PAUSED')"),
        postgresql_where=sa.text("status IN ('CREATED','QUEUED','RUNNING','PAUSING','PAUSED')"),
    )

    op.create_table(
        "idempotency_keys",
        sa.Column("key", sa.String(length=36), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=False),
        sa.Column("turn_id", sa.String(length=64), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_index(
        "ix_idempotency_keys_conversation",
        "idempotency_keys",
        ["conversation_id"],
        unique=False,
    )
    op.create_index(
        "ix_idempotency_keys_created_at",
        "idempotency_keys",
        ["created_at"],
        unique=False,
    )

    with op.batch_alter_table("conversations") as batch_op:
        batch_op.add_column(
            sa.Column("default_send_intent", sa.String(length=16), nullable=True),
        )


def downgrade() -> None:
    with op.batch_alter_table("conversations") as batch_op:
        batch_op.drop_column("default_send_intent")

    op.drop_index("ix_idempotency_keys_created_at", table_name="idempotency_keys")
    op.drop_index("ix_idempotency_keys_conversation", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")

    op.drop_index("ux_turns_one_active_per_conversation", table_name="turns")
    op.drop_index("ix_turns_conversation_created", table_name="turns")
    op.drop_index("ix_turns_live_lease", table_name="turns")
    op.drop_table("turns")
