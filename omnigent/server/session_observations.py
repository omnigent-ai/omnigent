"""Ordered projections of durable session observations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from threading import RLock
from typing import ParamSpec, TypeVar

from omnigent.db.workspace_cache import WorkspaceScopedCache, WorkspaceScopedSet
from omnigent.entities.conversation import Conversation
from omnigent.entities.session_observation import (
    SessionObservation,
    accept_session_projection,
    is_versioned_session_observation,
)
from omnigent.runtime import session_stream

_P = ParamSpec("_P")
_R = TypeVar("_R")


@dataclass(frozen=True)
class SessionProjection:
    observation: SessionObservation
    status: str
    offline_episode_id: str | None = None

    @property
    def order(self) -> tuple[int, int, int]:
        return (
            self.observation.binding_epoch,
            self.observation.revision,
            int(self.offline_episode_id is not None),
        )


class SessionStatusCache(WorkspaceScopedCache[str, str]):
    """A learned durable fence cannot be rewound by an older or legacy edge."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = RLock()
        self._projections: WorkspaceScopedCache[str, SessionProjection] = WorkspaceScopedCache()
        self._hydrated_only: WorkspaceScopedSet[str] = WorkspaceScopedSet()

    def __setitem__(self, key: str, value: str) -> None:
        with self._lock:
            if self._projections.get(key) is None:
                super().__setitem__(key, value)
                self._hydrated_only.discard(key)

    def synchronized(self, function: Callable[_P, _R]) -> Callable[_P, _R]:
        """Keep a projection and its companion live caches in one critical section."""

        @wraps(function)
        def guarded(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            with self._lock:
                return function(*args, **kwargs)

        return guarded

    def accept(self, key: str, projection: SessionProjection, *, published: bool = True) -> bool:
        with self._lock:
            return self._accept(key, projection, published=published)

    def _accept(self, key: str, projection: SessionProjection, *, published: bool) -> bool:
        previous = self._projections.get(key)
        was_missing = super().get(key) is None
        if previous is not None:
            session_stream.learn_observation(
                key, previous.observation, previous.offline_episode_id
            )
        if not accept_session_projection(
            projection.observation,
            projection.offline_episode_id,
            previous.observation if previous is not None else None,
            previous.offline_episode_id if previous is not None else None,
        ):
            return False
        if not session_stream.learn_observation(
            key, projection.observation, projection.offline_episode_id
        ):
            return False
        self._projections[key] = projection
        super().__setitem__(key, projection.status)
        if published:
            self._hydrated_only.discard(key)
        elif was_missing or previous != projection:
            self._hydrated_only.add(key)
        return True

    def has_live_status(self, key: str) -> bool:
        """A hydrated database value is not evidence of a live local publisher."""
        with self._lock:
            return super().get(key) is not None and key not in self._hydrated_only

    def projection(self, key: str) -> SessionProjection | None:
        with self._lock:
            return self._projections.get(key)

    def hydrate(self, conversation: Conversation) -> bool:
        observation = conversation.session_observation
        # Default-0 legacy rows have no durable observation yet.
        if not is_versioned_session_observation(observation):
            return False
        decision = conversation.runner_offline_decision
        return self.accept(
            conversation.id,
            SessionProjection(
                observation,
                conversation.live_status or "idle",
                decision.episode_id if decision is not None else None,
            ),
            published=False,
        )

    def clear(self) -> None:
        with self._lock:
            super().clear()
            self._projections.clear()
            self._hydrated_only.clear()


class SessionTerminalPendingCache(WorkspaceScopedCache[str, bool]):
    """Keep terminal startup ownership even before its status projection arrives."""

    def __init__(self) -> None:
        super().__init__()
        self._lock = RLock()
        self._owners: WorkspaceScopedCache[str, tuple[SessionObservation, str | None]] = (
            WorkspaceScopedCache()
        )

    def accept(
        self,
        key: str,
        pending: bool,
        observation: SessionObservation | None,
        episode_id: str | None,
    ) -> bool:
        with self._lock:
            return self._accept(key, pending, observation, episode_id)

    def _accept(
        self,
        key: str,
        pending: bool,
        observation: SessionObservation | None,
        episode_id: str | None,
    ) -> bool:
        previous = self._owners.get(key)
        if previous is not None:
            if not accept_session_projection(observation, episode_id, previous[0], previous[1]):
                return False
        if observation is not None:
            self._owners[key] = (observation, episode_id)
        if pending:
            super().__setitem__(key, True)
        else:
            super().pop(key, None)
        return True

    def hydrate(self, conversation: Conversation) -> None:
        with self._lock:
            self._hydrate(conversation)

    def _hydrate(self, conversation: Conversation) -> None:
        previous = self._owners.get(conversation.id)
        decision = conversation.runner_offline_decision
        episode_id = decision.episode_id if decision is not None else None
        if not is_versioned_session_observation(conversation.session_observation):
            return
        if previous is not None and not accept_session_projection(
            conversation.session_observation, episode_id, previous[0], previous[1]
        ):
            return
        self._owners[conversation.id] = (conversation.session_observation, episode_id)
        if conversation.terminal_pending:
            super().__setitem__(conversation.id, True)
        else:
            super().pop(conversation.id, None)

    def value_for(self, conversation: Conversation) -> bool:
        """Use the row's durable value, preserving only unversioned legacy flags."""
        if is_versioned_session_observation(conversation.session_observation):
            return conversation.terminal_pending
        with self._lock:
            owner = self._owners.get(conversation.id)
            if owner is None or owner[0] == conversation.session_observation:
                return super().get(conversation.id, conversation.terminal_pending)
            return conversation.terminal_pending

    def clear(self) -> None:
        with self._lock:
            super().clear()
            self._owners.clear()
