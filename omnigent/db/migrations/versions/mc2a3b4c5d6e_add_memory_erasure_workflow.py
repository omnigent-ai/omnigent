from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from omnigent.db.db_models import Uuid16

revision: str = "mc2a3b4c5d6e"
down_revision: str | None = "mc1a2b3c4d5e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "memory_erasure_requests",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("requested_by", sa.String(128), nullable=False),
        sa.Column("scope_kind", sa.String(16), nullable=False),
        sa.Column("scope_subject", sa.String(512), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("requested_at", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("completed_at", sa.Integer(), nullable=True),
        sa.Column("last_error", sa.String(128), nullable=True),
        sa.CheckConstraint(
            "scope_kind IN ('personal', 'conversation', 'org')",
            name="ck_memory_erasure_requests_scope_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'in_progress', 'completed', 'blocked', 'failed')",
            name="ck_memory_erasure_requests_status",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "operation_id",
            name="uq_memory_erasure_requests_operation",
        ),
    )
    op.create_index(
        "ix_memory_erasure_requests_subject",
        "memory_erasure_requests",
        ["workspace_id", "requested_by", "created_at", "id"],
    )
    op.create_table(
        "memory_erasure_tasks",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("erasure_id", Uuid16(), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("operation_id", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_attempt_at", sa.Integer(), nullable=False),
        sa.Column("lease_owner", sa.String(128), nullable=True),
        sa.Column("lease_expires_at", sa.Integer(), nullable=True),
        sa.Column("receipt_json", sa.LargeBinary(), nullable=True),
        sa.Column("last_error", sa.String(128), nullable=True),
        sa.Column("verified_at", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.Integer(), nullable=False),
        sa.Column("finished_at", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'retryable', 'completed', "
            "'unsupported', 'dead_letter', 'cancelled')",
            name="ck_memory_erasure_tasks_status",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "erasure_id",
            "provider",
            name="uq_memory_erasure_tasks_provider",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "operation_id",
            name="uq_memory_erasure_tasks_operation",
        ),
    )
    op.create_index(
        "ix_memory_erasure_tasks_claim",
        "memory_erasure_tasks",
        ["status", "next_attempt_at", "created_at", "workspace_id", "id"],
    )
    op.create_index(
        "ix_memory_erasure_tasks_request",
        "memory_erasure_tasks",
        ["workspace_id", "erasure_id", "provider"],
    )
    op.create_table(
        "memory_erasure_attempts",
        sa.Column("workspace_id", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("id", Uuid16(), nullable=False),
        sa.Column("task_id", Uuid16(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(128), nullable=True),
        sa.Column("started_at", sa.Integer(), nullable=False),
        sa.Column("finished_at", sa.Integer(), nullable=True),
        sa.CheckConstraint(
            "status IN ('running', 'completed', 'failed')",
            name="ck_memory_erasure_attempts_status",
        ),
        sa.PrimaryKeyConstraint("workspace_id", "id"),
        sa.UniqueConstraint(
            "workspace_id",
            "task_id",
            "attempt_number",
            name="uq_memory_erasure_attempts_number",
        ),
    )
    op.create_index(
        "ix_memory_erasure_attempts_task",
        "memory_erasure_attempts",
        ["workspace_id", "task_id", "attempt_number"],
    )


def downgrade() -> None:
    op.drop_index("ix_memory_erasure_attempts_task", table_name="memory_erasure_attempts")
    op.drop_table("memory_erasure_attempts")
    op.drop_index("ix_memory_erasure_tasks_request", table_name="memory_erasure_tasks")
    op.drop_index("ix_memory_erasure_tasks_claim", table_name="memory_erasure_tasks")
    op.drop_table("memory_erasure_tasks")
    op.drop_index("ix_memory_erasure_requests_subject", table_name="memory_erasure_requests")
    op.drop_table("memory_erasure_requests")
