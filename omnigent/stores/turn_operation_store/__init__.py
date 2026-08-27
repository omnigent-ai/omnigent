"""Persistence contract for durable, replay-safe session turn operations."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from omnigent.entities import TurnOperation


class TurnOperationConflict(ValueError):
    """An idempotency key was reused for a different canonical request."""


class TurnOperationStateError(ValueError):
    """A requested lifecycle transition conflicts with durable state."""


class TurnOperationStore(ABC):
    """Abstract store for the turn-operation journal."""

    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location

    @abstractmethod
    def create_or_get(
        self,
        *,
        conversation_id: str,
        principal: str,
        idempotency_key: str,
        request: dict[str, Any],
    ) -> tuple[TurnOperation, bool]:
        """Create an ``accepted`` operation or replay the existing row."""
        ...

    @abstractmethod
    def get(self, operation_id: str) -> TurnOperation | None:
        """Return one workspace-scoped operation by id."""
        ...

    @abstractmethod
    def mark_input_persisted(self, operation_id: str, item_id: str) -> TurnOperation:
        """Advance ``accepted`` to ``input_persisted`` idempotently."""
        ...

    @abstractmethod
    def mark_dispatch_result(
        self,
        operation_id: str,
        *,
        state: str,
        error_code: str | None = None,
        error: str | None = None,
    ) -> TurnOperation:
        """Record ``dispatched`` or the explicit ``dispatch_unknown`` ambiguity."""
        ...

    @abstractmethod
    def mark_terminal(
        self,
        operation_id: str,
        *,
        state: str,
        error_code: str | None = None,
        error: str | None = None,
    ) -> TurnOperation:
        """Advance a dispatched operation to a terminal state idempotently."""
        ...

    @abstractmethod
    def delete_for_conversation(self, conversation_id: str) -> int:
        """Delete journal rows when their owning conversation is deleted."""
        ...
