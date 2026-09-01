from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from omnigent.spec.types import MemoryProviderName, MemoryScopeKind

MemoryErasureRequestStatus = Literal[
    "pending",
    "in_progress",
    "completed",
    "blocked",
    "failed",
]
MemoryErasureTaskStatus = Literal[
    "pending",
    "processing",
    "retryable",
    "completed",
    "unsupported",
    "dead_letter",
    "cancelled",
]


@dataclass(frozen=True)
class MemoryErasure:
    id: str
    workspace_id: int
    operation_id: str
    requested_by: str
    scope_kind: MemoryScopeKind
    scope_subject: str | None
    status: MemoryErasureRequestStatus
    requested_at: int
    created_at: int
    updated_at: int
    completed_at: int | None
    last_error: str | None


@dataclass(frozen=True)
class MemoryErasureTask:
    id: str
    workspace_id: int
    erasure_id: str
    provider: MemoryProviderName
    operation_id: str
    status: MemoryErasureTaskStatus
    attempt_count: int
    max_attempts: int
    next_attempt_at: int
    lease_owner: str | None
    lease_expires_at: int | None
    receipt_json: str | None
    last_error: str | None
    verified_at: int | None
    created_at: int
    updated_at: int
    finished_at: int | None


@dataclass(frozen=True)
class MemoryErasureAttempt:
    id: str
    workspace_id: int
    task_id: str
    attempt_number: int
    status: str
    error_code: str | None
    started_at: int
    finished_at: int | None
