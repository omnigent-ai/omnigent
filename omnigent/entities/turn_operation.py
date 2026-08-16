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

    ``request_json`` is the canonical external request needed to resume an
    operation that crashed before input persistence. ``dispatch_request_json``
    freezes the exact runner envelope after persistence so routing or file
    resolution cannot silently change a retry. Idempotency keys and actor
    identities are stored only as SHA-256 digests.
    """

    id: str
    conversation_id: str
    principal_hash: str
    idempotency_key_hash: str
    request_hash: str
    request_json: str
    dispatch_request_hash: str | None
    dispatch_request_json: str | None
    state: str
    created_at: int
    workspace_id: int = 0
    item_id: str | None = None
    runner_incarnation_id: str | None = None
    dispatch_attempts: int = 0
    last_dispatch_at: int | None = None
    updated_at: int | None = None
    terminal_at: int | None = None
    error_code: str | None = None
    error: str | None = None
