"""Unit tests for runner-local turn-operation deduplication."""

from __future__ import annotations

import math
import uuid

import pytest

from omnigent.runner.turn_operations import (
    InvalidTurnOperation,
    RunnerTurnOperationRegistry,
    TurnOperationCapacityError,
    TurnOperationConflict,
    TurnOperationStateError,
)


def _op(seed: str) -> str:
    return uuid.uuid5(uuid.NAMESPACE_DNS, seed).hex


def test_exact_replay_returns_same_record_without_advancing() -> None:
    ticks = iter([10.0, 20.0, 30.0])
    registry = RunnerTurnOperationRegistry(clock=lambda: next(ticks))
    request = {"operation_id": _op("one"), "content": [{"text": "build"}]}
    first, created = registry.reserve(
        operation_id=_op("one"), session_id=_op("session"), request=request
    )
    running = registry.mark_running(first.operation_id)
    replay, replay_created = registry.reserve(
        operation_id=_op("one"), session_id=_op("session"), request=dict(request)
    )
    assert created is True
    assert replay_created is False
    assert replay == running
    assert replay.state == "running"


def test_operation_id_is_not_part_of_request_fingerprint() -> None:
    registry = RunnerTurnOperationRegistry()
    operation_id = _op("one")
    first, _ = registry.reserve(
        operation_id=operation_id,
        session_id=_op("session"),
        request={"operation_id": operation_id, "content": "same"},
    )
    replay, created = registry.reserve(
        operation_id=operation_id,
        session_id=_op("session"),
        request={"content": "same"},
    )
    assert created is False
    assert replay == first


@pytest.mark.parametrize("operation_id", ["", "ABC", "g" * 32, "a" * 31, "a" * 33])
def test_invalid_operation_id_fails_closed(operation_id: str) -> None:
    registry = RunnerTurnOperationRegistry()
    with pytest.raises(InvalidTurnOperation, match="32 lowercase"):
        registry.reserve(operation_id=operation_id, session_id=_op("session"), request={})


def test_changed_request_or_session_conflicts() -> None:
    registry = RunnerTurnOperationRegistry()
    operation_id = _op("one")
    registry.reserve(operation_id=operation_id, session_id=_op("session"), request={"x": 1})
    with pytest.raises(TurnOperationConflict):
        registry.reserve(operation_id=operation_id, session_id=_op("session"), request={"x": 2})
    with pytest.raises(TurnOperationConflict):
        registry.reserve(operation_id=operation_id, session_id=_op("other"), request={"x": 1})


def test_nonfinite_or_nonjson_request_is_rejected() -> None:
    registry = RunnerTurnOperationRegistry()
    with pytest.raises(InvalidTurnOperation, match="canonical JSON"):
        registry.reserve(
            operation_id=_op("nan"),
            session_id=_op("session"),
            request={"value": math.nan},
        )
    with pytest.raises(InvalidTurnOperation, match="canonical JSON"):
        registry.reserve(
            operation_id=_op("object"),
            session_id=_op("session"),
            request={"value": object()},
        )


def test_lifecycle_is_strict_terminal_and_idempotent() -> None:
    ticks = iter([10.0, 20.0, 30.0])
    registry = RunnerTurnOperationRegistry(clock=lambda: next(ticks))
    operation, _ = registry.reserve(operation_id=_op("one"), session_id=_op("session"), request={})
    with pytest.raises(TurnOperationStateError, match="accepted to succeeded"):
        registry.mark_terminal(operation.operation_id, "succeeded")
    running = registry.mark_running(operation.operation_id)
    assert running.updated_at == 20.0
    assert registry.mark_running(operation.operation_id) == running
    terminal = registry.mark_terminal(operation.operation_id, "succeeded")
    assert terminal.updated_at == 30.0
    assert registry.mark_terminal(operation.operation_id, "succeeded") == terminal
    with pytest.raises(TurnOperationStateError, match="succeeded to failed"):
        registry.mark_terminal(operation.operation_id, "failed")


def test_capacity_never_evicts_old_replay_records() -> None:
    registry = RunnerTurnOperationRegistry(max_operations=1)
    first, _ = registry.reserve(
        operation_id=_op("one"), session_id=_op("session"), request={"x": 1}
    )
    registry.mark_running(first.operation_id)
    registry.mark_terminal(first.operation_id, "succeeded")
    with pytest.raises(TurnOperationCapacityError, match="registry is full"):
        registry.reserve(operation_id=_op("two"), session_id=_op("session"), request={"x": 2})
    replay, created = registry.reserve(
        operation_id=first.operation_id, session_id=_op("session"), request={"x": 1}
    )
    assert created is False
    assert replay.state == "succeeded"


def test_status_shape_omits_request_hash() -> None:
    registry = RunnerTurnOperationRegistry(clock=lambda: 10.0)
    operation, _ = registry.reserve(
        operation_id=_op("one"), session_id=_op("session"), request={"secret": "value"}
    )
    status = operation.public_dict(runner_incarnation_id="runner-incarnation")
    assert status == {
        "operation_id": _op("one"),
        "session_id": _op("session"),
        "state": "accepted",
        "created_at": 10.0,
        "updated_at": 10.0,
        "runner_incarnation_id": "runner-incarnation",
    }
    assert "request_hash" not in status


def test_unknown_operation_is_none_and_mutation_raises() -> None:
    registry = RunnerTurnOperationRegistry()
    operation_id = _op("unknown")
    assert registry.get(operation_id) is None
    with pytest.raises(KeyError, match=operation_id):
        registry.mark_running(operation_id)
