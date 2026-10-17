"""Retained ordering fences for session lifecycle consumers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from omnigent.entities.session_observation import (
    SessionObservation,
    accept_session_projection,
    is_versioned_session_observation,
)

_CHILD_LIFECYCLE_FIELDS = frozenset(
    {
        "current_task_status",
        "current_task_id",
        "busy",
        "last_task_error",
        "pending_elicitations_count",
        "session_observation",
        "runner_offline_episode_id",
    }
)


@dataclass
class SessionProjectionFence:
    """Remember accepted ownership across subscriptions and snapshot refreshes."""

    observation: SessionObservation | None = None
    episode_id: str | None = None

    def accept(self, observation: SessionObservation | None, episode_id: str | None) -> bool:
        """Advance the fence only when the canonical projection order allows it."""
        if episode_id is not None and (not isinstance(episode_id, str) or not episode_id):
            return False
        if not accept_session_projection(
            observation, episode_id, self.observation, self.episode_id
        ):
            return False
        if is_versioned_session_observation(observation):
            self.observation = observation
            self.episode_id = episode_id
        return True

    def filter_child(self, child: dict[str, Any]) -> dict[str, Any]:
        """Keep metadata deltas while rejecting outdated child lifecycle fields."""
        if not _CHILD_LIFECYCLE_FIELDS.intersection(child):
            return child
        raw_observation = child.get("session_observation")
        try:
            observation = (
                SessionObservation.model_validate(raw_observation)
                if raw_observation is not None
                else None
            )
        except ValidationError:
            accepted = False
        else:
            accepted = self.accept(observation, child.get("runner_offline_episode_id"))
        if accepted:
            return child
        return {key: value for key, value in child.items() if key not in _CHILD_LIFECYCLE_FIELDS}

    def accept_signal(
        self, observation: SessionObservation | None, episode_id: str | None
    ) -> bool:
        """Keep legacy signals while requiring ownership for an annotated episode."""
        if observation is None:
            return episode_id is None
        return self.accept(observation, episode_id)


def accept_owned_child_activity(
    item: dict[str, Any], fences: dict[str, SessionProjectionFence]
) -> bool:
    """Fence annotated live child notifications while keeping historical output."""
    if (
        item.get("type") != "resource_event"
        or item.get("resource_type") != "session"
        or item.get("event_type") != "session.subagent.returned"
    ):
        return True
    resource = item.get("resource")
    if not isinstance(resource, dict) or "session_observation" not in resource:
        return True
    child_id = item.get("resource_id")
    if not isinstance(child_id, str):
        return False
    try:
        observation = SessionObservation.model_validate(resource.get("session_observation"))
    except ValidationError:
        return False
    return fences.setdefault(child_id, SessionProjectionFence()).accept(
        observation, resource.get("runner_offline_episode_id")
    )
