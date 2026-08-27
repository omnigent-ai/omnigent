"""Durable lifecycle record for one externally submitted session turn."""

from __future__ import annotations

from dataclasses import dataclass

TURN_OPERATION_ACTIVE_STATES = frozenset(
    {"accepted", "input_persisted", "dispatched", "dispatch_unknown"}
)
TURN_OPERATION_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled", "timed_out"})
TURN_OPERATION_STATES = TURN_OPERATION_ACTIVE_STATES | TURN_OPERATION_TERMINAL_STATES


@dataclass(frozen=True)
class TurnOperation:
    """A replay-safe, durable turn-operation journal entry.

    ``request_json`` is the canonical request envelope needed to resume an
    operation that crashed after acceptance but before input persistence.
    Idempotency keys and actor identities are stored only as SHA-256 digests.
    """

    id: str
    conversation_id: str
    principal_hash: str
    idempotency_key_hash: str
    request_hash: str
    request_json: str
    state: str
    created_at: int
    workspace_id: int = 0
    item_id: str | None = None
    updated_at: int | None = None
    terminal_at: int | None = None
    error_code: str | None = None
    error: str | None = None
