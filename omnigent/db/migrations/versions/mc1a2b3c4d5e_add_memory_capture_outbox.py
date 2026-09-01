from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "mc1a2b3c4d5e"
down_revision: str | None = "ga1b2c3d4e5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_capture_intents",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("source_item_id", Uuid16(), nullable=False),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("account_subject", sa.String(128), nullable=False),
        sa.Column("targets_json", sa.LargeBinary(), nullable=False),
        sa.Column("targets_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("response_id", sa.String(128), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'completed', 'cancelled')",
            name="ck_memory_capture_intents_status",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "source_item_id",
            name="uq_memory_capture_intents_source_item",
        ),
    )
    op.create_index(
        "ix_memory_capture_intents_expiry",
        "memory_capture_intents",
        ["workspace_id", "status", "expires_at"],
    )
    op.create_table(
        "memory_capture_jobs",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("intent_id", Uuid16(), nullable=False),
        sa.Column("conversation_id", Uuid16(), nullable=False),
        sa.Column("response_id", sa.String(128), nullable=False),
        sa.Column("source_item_id", Uuid16(), nullable=False),
        sa.Column("account_subject", sa.String(128), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("scope_kind", sa.String(16), nullable=False),
        sa.Column("scope_subject", sa.String(512), nullable=True),
        sa.Column("target_hash", sa.String(64), nullable=False),
        sa.Column("capture_mode", sa.String(16), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("phase", sa.String(16), nullable=False, server_default="extraction"),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_attempt_at", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.Integer(), nullable=True),
        sa.Column("payload_hash", sa.String(64), nullable=True),
        sa.Column("last_error", sa.LargeBinary(), nullable=True),
        sa.Column("receipt_json", sa.LargeBinary(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("finished_at", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "scope_kind IN ('personal', 'conversation', 'org')",
            name="ck_memory_capture_jobs_scope_kind",
        ),
        sa.CheckConstraint(
            "capture_mode IN ('review', 'automatic')",
            name="ck_memory_capture_jobs_capture_mode",
        ),
        sa.CheckConstraint(
            "phase IN ('extraction', 'write')",
            name="ck_memory_capture_jobs_phase",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'pending_review', "
            "'retryable', 'succeeded', 'dead_letter', 'cancelled')",
            name="ck_memory_capture_jobs_status",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "intent_id",
            "provider",
            "target_hash",
            name="uq_memory_capture_jobs_target",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "operation_id",
            name="uq_memory_capture_jobs_operation",
        ),
    )
    op.create_index(
        "ix_memory_capture_jobs_claim",
        "memory_capture_jobs",
        ["status", "next_attempt_at", "created_at", "workspace_id", "id"],
    )
    op.create_index(
        "ix_memory_capture_jobs_conversation",
        "memory_capture_jobs",
        ["workspace_id", "conversation_id", "response_id"],
    )
    op.create_table(
        "memory_capture_attempts",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("job_id", Uuid16(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.LargeBinary(), nullable=True),
        sa.Column("receipt_json", sa.LargeBinary(), nullable=True),
        sa.Column("started_at", sa.Integer(), nullable=False),
        sa.Column("finished_at", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "phase IN ('extraction', 'write')",
            name="ck_memory_capture_attempts_phase",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_memory_capture_attempts_status",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "job_id",
            "attempt_number",
            name="uq_memory_capture_attempts_number",
        ),
    )
    op.create_index(
        "ix_memory_capture_attempts_job",
        "memory_capture_attempts",
        ["workspace_id", "job_id", "attempt_number"],
    )
    op.create_table(
        "memory_capture_reviews",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("job_id", Uuid16(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("candidates_json", sa.LargeBinary(), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decision_reason", sa.String(512), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("decided_at", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'cancelled')",
            name="ck_memory_capture_reviews_status",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "job_id",
            name="uq_memory_capture_reviews_job",
        ),
    )
    op.create_index(
        "ix_memory_capture_reviews_status",
        "memory_capture_reviews",
        ["workspace_id", "status", "created_at", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_capture_reviews_status", table_name="memory_capture_reviews")
    op.drop_table("memory_capture_reviews")
    op.drop_index("ix_memory_capture_attempts_job", table_name="memory_capture_attempts")
    op.drop_table("memory_capture_attempts")
    op.drop_index("ix_memory_capture_jobs_conversation", table_name="memory_capture_jobs")
    op.drop_index("ix_memory_capture_jobs_claim", table_name="memory_capture_jobs")
    op.drop_table("memory_capture_jobs")
    op.drop_index("ix_memory_capture_intents_expiry", table_name="memory_capture_intents")
    op.drop_table("memory_capture_intents")
