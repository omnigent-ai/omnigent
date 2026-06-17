"""
Pure turn state-machine and failure-taxonomy logic.

This module is the transport-agnostic, DB-agnostic core of Phase 1
server-authoritative turns (issue #466,
``designs/PHASE1_SERVER_AUTHORITATIVE_TURNS.md``). It contains only
pure functions and constants: the legal status set, the transition
matrix, the failure taxonomy, the ``WORKER_*``-only routing gate, and
lease-expiry classification.

It deliberately has **no** database, asyncio, or tunnel dependencies so
it can be unit-tested exhaustively with a table of cases and reused
unchanged by both the server dispatch path and the runner-side
supervisor. The persistence layer that applies these rules under a
fenced compare-and-set lives in :mod:`omnigent.stores.turn_store`.

Two design rules this module encodes:

1. A client disconnect may only touch ``attached`` / ``last_client_seen``;
   it can **never** drive a status transition. That is why no transition
   in :data:`_LEGAL_TRANSITIONS` is keyed to a client event.
2. Only ``WORKER_*`` failure codes may feed vendor health/routing.
   :func:`is_worker_attributable` is the single gate, so transport and
   runner-loss noise can never bench a healthy vendor (the original
   misdiagnosis behind the incident).
"""

from __future__ import annotations

from typing import Final

# ── Turn statuses ──────────────────────────────────────────────────

#: Turn created and persisted, before it is accepted into a runner queue.
STATUS_CREATED: Final = "CREATED"
#: Accepted into a runner queue; awaiting a lease claim.
STATUS_QUEUED: Final = "QUEUED"
#: Lease claimed by a runner; work is executing.
STATUS_RUNNING: Final = "RUNNING"
#: Cooperative pause requested (Phase 4 forward-compat; unused in Phase 1).
STATUS_PAUSING: Final = "PAUSING"
#: Cooperative pause effective (Phase 4 forward-compat; unused in Phase 1).
STATUS_PAUSED: Final = "PAUSED"
#: Worker returned a result.
STATUS_COMPLETED: Final = "COMPLETED"
#: Worker boot/task error, or lease lost (RUNNER_LOST).
STATUS_FAILED: Final = "FAILED"
#: Explicit user/operator abort.
STATUS_CANCELLED: Final = "CANCELLED"

#: Every legal status value (mirrors the ``ck_turns_status`` CHECK
#: constraint on the ``turns`` table; keep the two in sync).
ALL_STATUSES: Final[frozenset[str]] = frozenset(
    {
        STATUS_CREATED,
        STATUS_QUEUED,
        STATUS_RUNNING,
        STATUS_PAUSING,
        STATUS_PAUSED,
        STATUS_COMPLETED,
        STATUS_FAILED,
        STATUS_CANCELLED,
    }
)

#: Terminal statuses: no outgoing transitions, and excluded from the
#: ``ux_turns_one_active_per_conversation`` partial-unique index so a
#: finished turn frees the per-conversation slot.
TERMINAL_STATUSES: Final[frozenset[str]] = frozenset(
    {STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED}
)

#: Non-terminal statuses that hold the per-conversation active slot.
#: Mirrors the partial-index predicate; the orphan sweeper considers
#: only the leased subset (:data:`SWEEPABLE_STATUSES`).
ACTIVE_STATUSES: Final[frozenset[str]] = ALL_STATUSES - TERMINAL_STATUSES

#: Statuses a runner can hold a live lease in, hence the only ones the
#: orphan sweeper may transition to ``FAILED(RUNNER_LOST)`` on expiry.
SWEEPABLE_STATUSES: Final[frozenset[str]] = frozenset({STATUS_RUNNING, STATUS_PAUSING})


# ── Legal transitions ──────────────────────────────────────────────
#
# Keyed by source status; value is the set of allowed destination
# statuses. Notably:
#   * No client event appears here — disconnects touch only liveness
#     columns, never status (design rule 1).
#   * The only writer of each transition is documented in the design
#     doc's state-machine table; this module enforces *legality*, while
#     :mod:`omnigent.stores.turn_store` enforces *ownership* via fenced
#     CAS.
_LEGAL_TRANSITIONS: Final[dict[str, frozenset[str]]] = {
    STATUS_CREATED: frozenset({STATUS_QUEUED, STATUS_RUNNING, STATUS_CANCELLED}),
    # CREATED -> RUNNING covers the interactive/attach dispatch that
    # claims a lease without an intermediate queue hop.
    STATUS_QUEUED: frozenset({STATUS_RUNNING, STATUS_CANCELLED}),
    STATUS_RUNNING: frozenset({STATUS_PAUSING, STATUS_COMPLETED, STATUS_FAILED, STATUS_CANCELLED}),
    # PAUSING/PAUSED are forward-compat (Phase 4). A paused turn can
    # resume to RUNNING, be torn down, or fail on lease loss.
    STATUS_PAUSING: frozenset({STATUS_PAUSED, STATUS_RUNNING, STATUS_FAILED, STATUS_CANCELLED}),
    STATUS_PAUSED: frozenset({STATUS_RUNNING, STATUS_CANCELLED, STATUS_FAILED}),
    # Terminal states have no outgoing transitions.
    STATUS_COMPLETED: frozenset(),
    STATUS_FAILED: frozenset(),
    STATUS_CANCELLED: frozenset(),
}


# ── Failure taxonomy ───────────────────────────────────────────────

#: Transport-layer failure: the client connection dropped. Set only on
#: the observation path — never on a RUNNING turn's status. Excluded
#: from vendor routing.
ERROR_TRANSPORT_DISCONNECT: Final = "TRANSPORT_DISCONNECT"
#: The runner's lease expired (heartbeat went stale). Set by the server
#: orphan sweeper. Excluded from vendor routing — this is infra health,
#: not a vendor fault (the original misdiagnosis).
ERROR_RUNNER_LOST: Final = "RUNNER_LOST"
#: The worker failed to boot/start. Vendor-attributable.
ERROR_WORKER_BOOT_FAILURE: Final = "WORKER_BOOT_FAILURE"
#: The worker started but the task itself errored. Vendor-attributable.
ERROR_WORKER_TASK_FAILURE: Final = "WORKER_TASK_FAILURE"
#: Explicit user/operator cancel. Not a fault; excluded from routing.
ERROR_CANCELLED: Final = "CANCELLED"

#: Every legal error code (mirrors the ``ck_turns_error_code`` CHECK
#: constraint; keep the two in sync).
ALL_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {
        ERROR_TRANSPORT_DISCONNECT,
        ERROR_RUNNER_LOST,
        ERROR_WORKER_BOOT_FAILURE,
        ERROR_WORKER_TASK_FAILURE,
        ERROR_CANCELLED,
    }
)

#: The subset of failure codes attributable to the worker/vendor, and
#: therefore the **only** codes permitted to feed vendor health/routing.
WORKER_ATTRIBUTABLE_ERROR_CODES: Final[frozenset[str]] = frozenset(
    {ERROR_WORKER_BOOT_FAILURE, ERROR_WORKER_TASK_FAILURE}
)


def is_terminal(status: str) -> bool:
    """
    Return whether *status* is a terminal state (no outgoing transitions).

    :param status: A turn status value, e.g. ``"RUNNING"``.
    :returns: ``True`` if the status is terminal.
    :raises ValueError: If *status* is not a known status.
    """
    _require_status(status)
    return status in TERMINAL_STATUSES


def is_valid_transition(from_status: str, to_status: str) -> bool:
    """
    Return whether ``from_status -> to_status`` is a legal transition.

    Legality is independent of *ownership*: a transition can be legal
    here yet still be rejected by the store's fenced compare-and-set
    because the caller does not hold the lease. This function answers
    only "is this edge in the state machine?".

    :param from_status: Current status, e.g. ``"QUEUED"``.
    :param to_status: Proposed next status, e.g. ``"RUNNING"``.
    :returns: ``True`` if the edge exists in :data:`_LEGAL_TRANSITIONS`.
    :raises ValueError: If either status is not a known status.
    """
    _require_status(from_status)
    _require_status(to_status)
    return to_status in _LEGAL_TRANSITIONS[from_status]


def validate_transition(from_status: str, to_status: str) -> None:
    """
    Raise unless ``from_status -> to_status`` is a legal transition.

    Use at call sites that want a hard failure on an illegal edge rather
    than a boolean. The store layer calls this before issuing a CAS so an
    illegal transition is a programming error caught early, distinct from
    a *lost-lease* CAS miss (which is an expected runtime condition).

    :param from_status: Current status.
    :param to_status: Proposed next status.
    :raises ValueError: If the transition is illegal or a status is
        unknown.
    """
    if not is_valid_transition(from_status, to_status):
        raise ValueError(
            f"illegal turn transition {from_status!r} -> {to_status!r}; "
            f"legal targets from {from_status!r} are "
            f"{sorted(_LEGAL_TRANSITIONS[from_status])}"
        )


def is_worker_attributable(error_code: str | None) -> bool:
    """
    Return whether *error_code* may feed vendor health/routing.

    This is the single inviolable gate behind the failure-taxonomy rule:
    only ``WORKER_*`` codes are vendor-attributable. ``TRANSPORT_DISCONNECT``,
    ``RUNNER_LOST``, ``CANCELLED`` and ``None`` all return ``False`` so
    transport/infra noise can never bench a healthy vendor.

    :param error_code: A failure taxonomy value, or ``None`` for a
        successful / non-failed turn.
    :returns: ``True`` only for worker-attributable failure codes.
    :raises ValueError: If *error_code* is a non-``None`` value outside
        the taxonomy (a typo'd code must fail loud, not silently slip
        past the routing filter).
    """
    if error_code is None:
        return False
    if error_code not in ALL_ERROR_CODES:
        raise ValueError(
            f"unknown error_code {error_code!r}; expected one of {sorted(ALL_ERROR_CODES)} or None"
        )
    return error_code in WORKER_ATTRIBUTABLE_ERROR_CODES


def is_lease_expired(lease_expires_at: int | None, now: int) -> bool:
    """
    Return whether a lease is expired as of *now*.

    A turn with no lease set (``lease_expires_at is None``) is treated as
    **not** expired — it has not yet been claimed, so it is not a sweep
    candidate. The orphan sweeper additionally restricts itself to
    :data:`SWEEPABLE_STATUSES`; this helper answers only the time
    comparison.

    Time is always supplied by the caller (the server's ``now_epoch()``)
    rather than read internally, so a single clock governs both the
    heartbeat write and the sweep comparison and there is no cross-host
    skew. See the design doc's clock-skew risk note.

    :param lease_expires_at: Epoch seconds the lease expires, or ``None``
        if no lease is held.
    :param now: Current epoch seconds, supplied by the caller.
    :returns: ``True`` if a lease is held and ``lease_expires_at < now``.
    """
    if lease_expires_at is None:
        return False
    return lease_expires_at < now


def _require_status(status: str) -> None:
    """
    Raise :class:`ValueError` if *status* is not a known status.

    :param status: The status value to validate.
    :raises ValueError: If *status* is not in :data:`ALL_STATUSES`.
    """
    if status not in ALL_STATUSES:
        raise ValueError(f"unknown turn status {status!r}; expected one of {sorted(ALL_STATUSES)}")
