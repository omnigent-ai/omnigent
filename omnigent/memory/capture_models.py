from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from omnigent.memory.models import MemoryScope
from omnigent.spec.types import MemoryProviderName

MemoryCaptureMode = Literal["review", "automatic"]
MemoryCapturePhase = Literal["extraction", "write"]
MemoryCaptureJobStatus = Literal[
    "pending",
    "processing",
    "pending_review",
    "retryable",
    "succeeded",
    "dead_letter",
    "cancelled",
]
MemoryCaptureReviewStatus = Literal["pending", "approved", "rejected", "cancelled"]


@dataclass(frozen=True)
class MemoryCaptureTarget:
    provider: MemoryProviderName
    scope: MemoryScope
    capture_mode: MemoryCaptureMode
    policy_hash: str


@dataclass(frozen=True)
class MemoryCaptureIntent:
    id: str
    workspace_id: int
    source_item_id: str
    conversation_id: str
    account_subject: str
    targets: tuple[MemoryCaptureTarget, ...]
    targets_hash: str
    status: str
    response_id: str | None
    created_at: int
    expires_at: int
    completed_at: int | None


@dataclass(frozen=True)
class MemoryCaptureJob:
    id: str
    workspace_id: int
    intent_id: str
    conversation_id: str
    response_id: str
    source_item_id: str
    account_subject: str
    provider: MemoryProviderName
    scope: MemoryScope
    capture_mode: MemoryCaptureMode
    policy_hash: str
    policy_version: int
    phase: MemoryCapturePhase
    status: MemoryCaptureJobStatus
    operation_id: str
    attempt_count: int
    max_attempts: int
    next_attempt_at: int
    lease_owner: str | None
    lease_expires_at: int | None
    payload_hash: str | None
    last_error: str | None
    receipt_json: str | None
    created_at: int
    updated_at: int
    finished_at: int | None


@dataclass(frozen=True)
class MemoryCandidate:
    kind: Literal["fact", "preference", "decision", "entity", "relationship", "procedure"]
    text: str
    confidence: float
    sensitivity: Literal["public", "internal", "personal", "sensitive"]
    source_item_ids: tuple[str, ...]


@dataclass(frozen=True)
class MemoryCaptureReview:
    id: str
    workspace_id: int
    job_id: str
    status: MemoryCaptureReviewStatus
    candidates: tuple[MemoryCandidate, ...]
    requested_by: str
    decided_by: str | None
    decision_reason: str | None
    created_at: int
    decided_at: int | None


@dataclass(frozen=True)
class MemoryCaptureAttempt:
    id: str
    workspace_id: int
    job_id: str
    attempt_number: int
    phase: MemoryCapturePhase
    status: str
    error: str | None
    receipt_json: str | None
    started_at: int
    finished_at: int | None


@dataclass(frozen=True)
class MemoryCaptureRequest:
    operation_id: str
    scope: MemoryScope
    facts: tuple[str, ...]
    captured_at: int


@dataclass(frozen=True)
class MemoryCaptureReceipt:
    provider: MemoryProviderName
    operation_id: str
    scope: MemoryScope
    added: int
    revision: str
    updated_at: int


class MemoryCaptureProvider(Protocol):
    async def capture(self, request: MemoryCaptureRequest) -> MemoryCaptureReceipt: ...


@dataclass(frozen=True)
class MemoryEraseRequest:
    operation_id: str
    scope: MemoryScope
    erased_at: int


@dataclass(frozen=True)
class MemoryEraseReceipt:
    provider: MemoryProviderName
    operation_id: str
    scope_hash: str
    erased_revisions: int
    tombstoned_operations: int
    completed_at: int


class MemoryEraseProvider(Protocol):
    async def erase(self, request: MemoryEraseRequest) -> MemoryEraseReceipt: ...

    async def verify_erased(self, request: MemoryEraseRequest) -> bool: ...
