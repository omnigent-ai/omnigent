"""Durable ownership of live session observations and offline decisions."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

SESSION_ERROR_LABEL_KEYS = frozenset(
    f"omnigent.last_task_error_{name}"
    for name in (
        "code",
        "message",
        "agent_name",
        "title",
        "cause",
        "remediation",
        "item_id",
        "runner_id",
    )
)


class SessionObservation(BaseModel):
    """One ordered live-state observation within a runner binding."""

    model_config = ConfigDict(frozen=True)

    runner_id: str | None = None
    binding_epoch: int = Field(default=0, ge=0, strict=True)
    revision: int = Field(default=0, ge=0, strict=True)
    observation_id: str | None = None
    status: str | None = None
    response_id: str | None = None
    error_key: str | None = None


class TerminalStartupClaim(BaseModel):
    """One immutable reservation and the exact normal-state lineage it protects."""

    model_config = ConfigDict(frozen=True)

    reservation: SessionObservation
    observation: SessionObservation
    completed: bool = False


def is_versioned_session_observation(observation: SessionObservation | None) -> bool:
    """Default-0 legacy rows have no ordering fence until first observation."""
    return observation is not None and (
        observation.binding_epoch > 0
        or observation.revision > 0
        or observation.observation_id is not None
    )


def accept_session_projection(
    incoming: SessionObservation | None,
    incoming_episode_id: str | None,
    previous: SessionObservation | None,
    previous_episode_id: str | None,
) -> bool:
    """Order normal and owned-failure effects without rewinding a learned fence."""
    if not is_versioned_session_observation(previous):
        return True
    if not is_versioned_session_observation(incoming):
        return False
    assert incoming is not None and previous is not None
    if (
        incoming.binding_epoch == previous.binding_epoch
        and incoming.revision == previous.revision
        and incoming != previous
    ):
        return False
    incoming_order = (
        incoming.binding_epoch,
        incoming.revision,
        int(incoming_episode_id is not None),
    )
    previous_order = (
        previous.binding_epoch,
        previous.revision,
        int(previous_episode_id is not None),
    )
    if incoming_order != previous_order:
        return incoming_order > previous_order
    return incoming == previous and incoming_episode_id == previous_episode_id


class RunnerOfflineDecision(BaseModel):
    """An interruption attributed to one immutable observation."""

    model_config = ConfigDict(frozen=True)

    observation: SessionObservation
    episode_id: str
    error_labels: dict[str, str]
    activity_turn_id: str | None = None
    scheduled_run_id: str | None = None

    def matches(self, observation: SessionObservation) -> bool:
        """Whether this decision still describes the current observation."""
        return self.observation == observation


class RecordedRunnerOfflineDecision(BaseModel):
    """An idempotent settlement result, including whether it created the decision."""

    model_config = ConfigDict(frozen=True)

    decision: RunnerOfflineDecision
    created: bool
