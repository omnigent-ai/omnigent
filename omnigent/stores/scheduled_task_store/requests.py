"""Durable, owner-scoped receipts for committed scheduled-task mutations."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any

from omnigent.db.account_authority import account_generation
from omnigent.errors import ErrorCode, OmnigentError


def request_owner_key(owner: str) -> str:
    """Keep request IDs private to an authenticated account incarnation."""
    identity = json.dumps([owner, account_generation(owner)], separators=(",", ":"))
    return uuid.uuid5(uuid.NAMESPACE_URL, "omnigent:scheduled-task-request:" + identity).hex


@dataclass(frozen=True)
class MutationRequest:
    """Caller identity and the exact requested mutation, before resource resolution."""

    owner_key: str
    request_id: str
    request_hash: bytes

    @classmethod
    def create(
        cls, owner: str, request_id: str, action: str, target: str | None, payload: dict[str, Any]
    ) -> MutationRequest:
        """Normalize a UUID and bind it to the action, target and input fields."""
        try:
            canonical_id = uuid.UUID(request_id).hex
        except (ValueError, AttributeError) as exc:
            raise OmnigentError(
                "Idempotency-Key must be a UUID", code=ErrorCode.INVALID_INPUT
            ) from exc
        encoded = json.dumps([action, target, payload], sort_keys=True, separators=(",", ":"))
        return cls(
            request_owner_key(owner), canonical_id, hashlib.sha256(encoded.encode()).digest()
        )


@dataclass(frozen=True)
class MutationReceipt:
    """Proof that a request and its task mutation committed in one transaction."""

    request_id: str
    request_hash: bytes
    scheduled_task_id: str
    created_at: int

    def require_match(self, request: MutationRequest) -> None:
        """Reject reusing a key for a different operation without applying it."""
        if self.request_hash != request.request_hash:
            raise OmnigentError(
                "Idempotency-Key already applied to different inputs. No new mutation was "
                "applied. Look up the original request; use a new key only for a new intent.",
                code=ErrorCode.CONFLICT,
            )


class MutationAlreadyApplied(Exception):
    """Stop before changing a task when the receipt already exists."""

    def __init__(self, receipt: MutationReceipt) -> None:
        super().__init__("Scheduled-task request already applied")
        self.receipt = receipt
