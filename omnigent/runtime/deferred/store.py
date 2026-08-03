"""
Storage interface and memory implementation for deferred actions and audit events.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Sequence

from omnigent.runtime.deferred.models import DeferredAction, DeferredAuditEvent


class DeferredActionStore(ABC):
    """
    Abstract storage interface for deferred actions and audit logs.
    """

    @abstractmethod
    async def save_action(self, action: DeferredAction) -> None:
        """Save or update a deferred action in storage."""

    @abstractmethod
    async def get_action(self, action_id: str) -> DeferredAction | None:
        """Fetch a deferred action by ID."""

    @abstractmethod
    async def list_actions_for_session(self, session_id: str) -> list[DeferredAction]:
        """List all deferred actions associated with a session."""

    @abstractmethod
    async def add_audit_event(self, event: DeferredAuditEvent) -> None:
        """Append an audit event to the action's log."""

    @abstractmethod
    async def list_audit_events(self, action_id: str) -> list[DeferredAuditEvent]:
        """List all audit log events for an action."""


class MemoryDeferredActionStore(DeferredActionStore):
    """
    Thread-safe in-memory store for deferred actions and audit trails.
    """

    def __init__(self) -> None:
        self._actions: dict[str, DeferredAction] = {}
        self._audit_events: dict[str, list[DeferredAuditEvent]] = {}
        self._lock = asyncio.Lock()

    async def save_action(self, action: DeferredAction) -> None:
        """Store or overwrite a deferred action."""
        async with self._lock:
            self._actions[action.id] = action

    async def get_action(self, action_id: str) -> DeferredAction | None:
        """Retrieve action by ID."""
        async with self._lock:
            return self._actions.get(action_id)

    async def list_actions_for_session(self, session_id: str) -> list[DeferredAction]:
        """Return all actions registered for a session ID."""
        async with self._lock:
            return [
                action
                for action in self._actions.values()
                if action.session_id == session_id
            ]

    async def add_audit_event(self, event: DeferredAuditEvent) -> None:
        """Append audit event row."""
        async with self._lock:
            if event.action_id not in self._audit_events:
                self._audit_events[event.action_id] = []
            self._audit_events[event.action_id].append(event)

    async def list_audit_events(self, action_id: str) -> list[DeferredAuditEvent]:
        """Fetch chronological audit events for action_id."""
        async with self._lock:
            return list(self._audit_events.get(action_id, []))


_global_store: DeferredActionStore | None = None


def get_deferred_store() -> DeferredActionStore:
    """Get or initialize the global memory store instance."""
    global _global_store
    if _global_store is None:
        _global_store = MemoryDeferredActionStore()
    return _global_store


def set_deferred_store(store: DeferredActionStore | None) -> None:
    """Set global store instance (primarily for testing)."""
    global _global_store
    _global_store = store
