"""Tests for the pure turn state-machine and failure taxonomy.

Covers :mod:`omnigent.runtime.turn_state` (issue #466,
``designs/PHASE1_SERVER_AUTHORITATIVE_TURNS.md``). These are pure
functions with no DB or transport, so the suite is an exhaustive,
table-driven check of:

1. The transition matrix: every legal edge is accepted, and a
   representative set of illegal edges (including all
   terminal -> anything) is rejected.
2. No transition is keyed to a client event (encoded by the absence of
   any client trigger in the matrix; asserted indirectly via the
   liveness store test).
3. The ``WORKER_*``-only routing gate: transport/runner/cancel codes are
   excluded so they can never bench a healthy vendor.
4. Lease-expiry classification with an injected clock.
"""

from __future__ import annotations

import itertools

import pytest

from omnigent.runtime import turn_state as ts


def test_all_statuses_partition_into_terminal_and_active() -> None:
    """Terminal and active status sets partition the full status set.

    A status that is neither (or both) would let a turn fall outside the
    state machine — e.g. an "active" terminal status would hold the
    per-conversation slot forever.
    """
    assert ts.TERMINAL_STATUSES | ts.ACTIVE_STATUSES == ts.ALL_STATUSES
    assert frozenset() == ts.TERMINAL_STATUSES & ts.ACTIVE_STATUSES


def test_sweepable_statuses_are_active_and_leasable() -> None:
    """Only RUNNING/PAUSING are sweepable, and both are active (non-terminal).

    The sweeper must never touch a terminal turn (would corrupt a
    finished record) nor a not-yet-leased one (CREATED/QUEUED have no
    lease to expire).
    """
    assert frozenset({ts.STATUS_RUNNING, ts.STATUS_PAUSING}) == ts.SWEEPABLE_STATUSES
    assert ts.SWEEPABLE_STATUSES <= ts.ACTIVE_STATUSES


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        (ts.STATUS_CREATED, ts.STATUS_QUEUED),
        (ts.STATUS_CREATED, ts.STATUS_RUNNING),
        (ts.STATUS_CREATED, ts.STATUS_CANCELLED),
        (ts.STATUS_QUEUED, ts.STATUS_RUNNING),
        (ts.STATUS_QUEUED, ts.STATUS_CANCELLED),
        (ts.STATUS_RUNNING, ts.STATUS_COMPLETED),
        (ts.STATUS_RUNNING, ts.STATUS_FAILED),
        (ts.STATUS_RUNNING, ts.STATUS_CANCELLED),
        (ts.STATUS_RUNNING, ts.STATUS_PAUSING),
        (ts.STATUS_PAUSING, ts.STATUS_PAUSED),
        (ts.STATUS_PAUSING, ts.STATUS_RUNNING),
        (ts.STATUS_PAUSED, ts.STATUS_RUNNING),
        (ts.STATUS_PAUSED, ts.STATUS_FAILED),
    ],
)
def test_legal_transitions_accepted(from_status: str, to_status: str) -> None:
    """Each documented legal edge validates without raising."""
    assert ts.is_valid_transition(from_status, to_status)
    ts.validate_transition(from_status, to_status)  # must not raise


@pytest.mark.parametrize("terminal", sorted(ts.TERMINAL_STATUSES))
def test_terminal_states_have_no_outgoing_transitions(terminal: str) -> None:
    """No terminal status may transition anywhere — the record is final."""
    assert ts.is_terminal(terminal)
    for target in ts.ALL_STATUSES:
        assert not ts.is_valid_transition(terminal, target), (
            f"terminal {terminal!r} must not transition to {target!r}"
        )


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        # Can't skip back to an earlier non-terminal phase.
        (ts.STATUS_RUNNING, ts.STATUS_QUEUED),
        (ts.STATUS_QUEUED, ts.STATUS_CREATED),
        # QUEUED can't jump straight to a non-cancel terminal.
        (ts.STATUS_QUEUED, ts.STATUS_COMPLETED),
        # CREATED can't complete without running.
        (ts.STATUS_CREATED, ts.STATUS_COMPLETED),
        # Self-loops are not transitions.
        (ts.STATUS_RUNNING, ts.STATUS_RUNNING),
    ],
)
def test_illegal_transitions_rejected(from_status: str, to_status: str) -> None:
    """Representative illegal edges are rejected by both predicates."""
    assert not ts.is_valid_transition(from_status, to_status)
    with pytest.raises(ValueError, match="illegal turn transition"):
        ts.validate_transition(from_status, to_status)


def test_no_client_event_drives_a_transition() -> None:
    """The matrix is keyed only on statuses, never a client connect/disconnect.

    This is design rule 1 at the type level: there is no edge a client
    event could traverse, because attach/detach are not statuses. The
    store-level guarantee (liveness writes touch only two columns) is
    covered in the TurnStore tests.
    """
    # Every key and every target is a real status — there is no
    # "ATTACHED"/"DETACHED" pseudo-status that a disconnect could push.
    for src, targets in ts._LEGAL_TRANSITIONS.items():
        assert src in ts.ALL_STATUSES
        assert targets <= ts.ALL_STATUSES


@pytest.mark.parametrize("unknown", ["", "running", "DONE", "ATTACHED"])
def test_unknown_status_raises(unknown: str) -> None:
    """An out-of-set status is a programming error and must fail loud."""
    with pytest.raises(ValueError, match="unknown turn status"):
        ts.is_terminal(unknown)


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        (ts.ERROR_WORKER_BOOT_FAILURE, True),
        (ts.ERROR_WORKER_TASK_FAILURE, True),
        (ts.ERROR_TRANSPORT_DISCONNECT, False),
        (ts.ERROR_RUNNER_LOST, False),
        (ts.ERROR_CANCELLED, False),
        (None, False),
    ],
)
def test_is_worker_attributable(code: str | None, expected: bool) -> None:
    """Only WORKER_* codes feed vendor routing.

    The transport/runner/cancel codes returning False is the fix for the
    original incident, where transport noise benched a healthy vendor.
    """
    assert ts.is_worker_attributable(code) is expected


def test_is_worker_attributable_rejects_unknown_code() -> None:
    """A typo'd code raises rather than silently slipping past the gate."""
    with pytest.raises(ValueError, match="unknown error_code"):
        ts.is_worker_attributable("WORKER_OOPS")


def test_only_worker_codes_in_attributable_set() -> None:
    """The attributable set is exactly the two WORKER_* codes."""
    assert (
        frozenset({ts.ERROR_WORKER_BOOT_FAILURE, ts.ERROR_WORKER_TASK_FAILURE})
        == ts.WORKER_ATTRIBUTABLE_ERROR_CODES
    )
    assert ts.WORKER_ATTRIBUTABLE_ERROR_CODES <= ts.ALL_ERROR_CODES


@pytest.mark.parametrize(
    ("lease_expires_at", "now", "expected"),
    [
        (None, 100, False),  # unclaimed -> never expired
        (100, 100, False),  # exactly now -> not yet expired
        (100, 101, True),  # past expiry -> expired
        (200, 100, False),  # future expiry -> live
    ],
)
def test_is_lease_expired(lease_expires_at: int | None, now: int, expected: bool) -> None:
    """Lease expiry is a strict ``expires_at < now`` with an injected clock."""
    assert ts.is_lease_expired(lease_expires_at, now) is expected


def test_transition_matrix_is_closed_over_all_statuses() -> None:
    """Every status appears as a transition source (the matrix is total).

    A missing key would make ``is_valid_transition`` raise for a real
    status rather than return False, which would surface as a confusing
    crash instead of a clean rejection.
    """
    assert set(ts._LEGAL_TRANSITIONS.keys()) == set(ts.ALL_STATUSES)
    # And every (src, dst) pair is answerable without error.
    for src, dst in itertools.product(ts.ALL_STATUSES, repeat=2):
        assert isinstance(ts.is_valid_transition(src, dst), bool)
