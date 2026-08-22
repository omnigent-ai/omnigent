"""Fail-closed runner-local deduplication for durable turn operation IDs."""

from __future__ import annotations

import hashlib
import json
import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from typing import Any

_OPERATION_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


class InvalidTurnOperation(ValueError):
    """An operation identifier or request envelope is malformed."""


class TurnOperationConflict(ValueError):
    """An operation identifier was rebound or its request changed."""


class TurnOperationCapacityError(RuntimeError):
    """The fail-closed in-process replay registry reached its bound."""


class TurnOperationStateError(RuntimeError):
    """A lifecycle transition contradicts the runner's observed state."""


@dataclass(frozen=True)
class RunnerTurnOperation:
    """Runner-observed state for one server-owned operation identifier."""

    operation_id: str
    session_id: str
    request_hash: str
    state: str
    created_at: float
    updated_at: float

    def public_dict(self, *, runner_incarnation_id: str) -> dict[str, str | float]:
        """Return the bounded status shape exposed to the server coordinator."""
        return {
            "operation_id": self.operation_id,
            "session_id": self.session_id,
            "state": self.state,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "runner_incarnation_id": runner_incarnation_id,
        }


def _canonical_hash(request: Mapping[str, Any]) -> str:
    body = dict(request)
    body.pop("operation_id", None)
    try:
        encoded = json.dumps(
            body,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    except (TypeError, ValueError) as exc:
        raise InvalidTurnOperation("operation request must be canonical JSON data") from exc
    return hashlib.sha256(b"omnigent.runner-turn/v1\0" + encoded).hexdigest()


class RunnerTurnOperationRegistry:
    """Exact in-process replay registry with a fail-closed capacity bound.

    Records are never evicted during a runner incarnation: eviction would let
    an old operation ID execute twice. A caller that reaches ``max_operations``
    receives a capacity error instead. A runner restart creates a new
    ``runner_incarnation_id``; the server must treat a missing old operation as
    ambiguous and must not blindly redispatch it.
    """

    def __init__(
        self,
        *,
        max_operations: int = 100_000,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if max_operations < 1:
            raise ValueError("max_operations must be positive")
        self._max_operations = max_operations
        self._clock = clock
        self._records: dict[str, RunnerTurnOperation] = {}

    @staticmethod
    def validate_operation_id(operation_id: str) -> None:
        if not _OPERATION_ID_RE.fullmatch(operation_id):
            raise InvalidTurnOperation("operation_id must be 32 lowercase hexadecimal characters")

    def reserve(
        self,
        *,
        operation_id: str,
        session_id: str,
        request: Mapping[str, Any],
    ) -> tuple[RunnerTurnOperation, bool]:
        """Reserve an operation or return its exact replay record."""
        self.validate_operation_id(operation_id)
        request_hash = _canonical_hash(request)
        existing = self._records.get(operation_id)
        if existing is not None:
            if existing.session_id != session_id or existing.request_hash != request_hash:
                raise TurnOperationConflict(
                    "operation_id is already bound to another session or request"
                )
            return existing, False
        if len(self._records) >= self._max_operations:
            raise TurnOperationCapacityError("runner operation replay registry is full")
        now = self._clock()
        created = RunnerTurnOperation(
            operation_id=operation_id,
            session_id=session_id,
            request_hash=request_hash,
            state="accepted",
            created_at=now,
            updated_at=now,
        )
        self._records[operation_id] = created
        return created, True

    def get(self, operation_id: str) -> RunnerTurnOperation | None:
        """Return one operation without changing its lifecycle."""
        self.validate_operation_id(operation_id)
        return self._records.get(operation_id)

    def mark_running(self, operation_id: str) -> RunnerTurnOperation:
        """Advance ``accepted`` to ``running``; exact replay is a no-op."""
        return self._transition(operation_id, expected=frozenset({"accepted"}), target="running")

    def mark_terminal(self, operation_id: str, state: str) -> RunnerTurnOperation:
        """Advance ``running`` to an immutable terminal state."""
        if state not in _TERMINAL_STATES:
            raise ValueError("runner terminal state is invalid")
        return self._transition(operation_id, expected=frozenset({"running"}), target=state)

    def _transition(
        self,
        operation_id: str,
        *,
        expected: frozenset[str],
        target: str,
    ) -> RunnerTurnOperation:
        record = self.get(operation_id)
        if record is None:
            raise KeyError(operation_id)
        if record.state == target:
            return record
        if record.state not in expected:
            raise TurnOperationStateError(
                f"cannot transition operation {operation_id} from {record.state} to {target}"
            )
        advanced = replace(record, state=target, updated_at=self._clock())
        self._records[operation_id] = advanced
        return advanced
