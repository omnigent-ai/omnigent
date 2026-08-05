from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from issue_prioritization.artifacts import RankedIssue
from issue_prioritization.domain import Priority, Severity
from issue_prioritization.labels import LabelManifest


@dataclass(frozen=True)
class BotState:
    issue_number: int
    priority: str | None
    severity: str | None
    components: tuple[str, ...]

    @property
    def has_ownership(self) -> bool:
        return self.priority is not None or self.severity is not None or bool(self.components)


class BotStateRepository(Protocol):
    def load(self) -> dict[int, BotState]: ...

    def upsert(self, states: list[BotState]) -> None: ...


class LegacyPriorityOwnership(Protocol):
    def is_bot_owned(self, issue_number: int, priority: str) -> bool: ...


@dataclass(frozen=True)
class MutationTarget:
    issue_number: int
    priority: str
    severity: str
    components: tuple[str, ...]


@dataclass(frozen=True)
class MutationPlan:
    target: MutationTarget
    labels_add: tuple[str, ...]
    labels_remove: tuple[str, ...]
    blocked: tuple[str, ...]
    next_state: BotState


class MutationPlanner:
    def __init__(
        self,
        manifest: LabelManifest,
        states: BotStateRepository,
        legacy_priorities: LegacyPriorityOwnership | None = None,
    ) -> None:
        self.manifest = manifest
        self.states = states
        self.legacy_priorities = legacy_priorities
        self.priority_labels = {priority.value for priority in Priority}

    def plan_all(
        self,
        ranked: tuple[RankedIssue, ...],
        current_labels: dict[int, tuple[str, ...]],
        states: dict[int, BotState] | None = None,
    ) -> tuple[MutationPlan, ...]:
        states = states if states is not None else self.load_states()
        plans = []
        for item in ranked:
            labels = current_labels.get(item.issue.number, ())
            state = self.resolve_state(item.issue.number, labels, states.get(item.issue.number))
            plans.append(self.plan_one(target_from_ranked(item), labels, state))
        return tuple(plans)

    def load_states(self) -> dict[int, BotState]:
        return self.states.load()

    def resolve_state(
        self,
        issue_number: int,
        current_labels: tuple[str, ...],
        state: BotState | None,
    ) -> BotState | None:
        if state is not None or self.legacy_priorities is None:
            return state
        priorities = set(current_labels) & self.priority_labels
        if len(priorities) != 1:
            return None
        priority = next(iter(priorities))
        if not self.legacy_priorities.is_bot_owned(issue_number, priority):
            return None
        return BotState(issue_number, priority, None, ())

    def severity_override(
        self,
        current_labels: tuple[str, ...],
        state: BotState | None,
    ) -> Severity | None:
        labels = set(current_labels) & self.manifest.severity_labels
        if len(labels) != 1:
            return None
        label = next(iter(labels))
        if state is not None and label == state.severity:
            return None
        return Severity(label.removeprefix("severity:"))

    def plan_one(
        self,
        target: MutationTarget,
        current_labels: tuple[str, ...],
        state: BotState | None,
    ) -> MutationPlan:
        existing = set(current_labels)
        labels_add: set[str] = set()
        labels_remove: set[str] = set()
        blocked: list[str] = []

        current_priorities = existing & self.priority_labels
        current_priority = next(iter(current_priorities)) if len(current_priorities) == 1 else None
        priority_written = False
        priority_owned = not current_priorities or (
            state is not None and current_priority == state.priority
        )
        if len(current_priorities) > 1:
            blocked.append("priority_label_conflict")
        elif current_priority != target.priority:
            if priority_owned:
                labels_add.add(target.priority)
                priority_written = True
                if current_priority:
                    labels_remove.add(current_priority)
            else:
                blocked.append("priority_human_override")

        current_severities = existing & self.manifest.severity_labels
        current_severity = next(iter(current_severities)) if len(current_severities) == 1 else None
        severity_written = False
        severity_owned = not current_severities or (
            state is not None and current_severity == state.severity
        )
        if len(current_severities) > 1:
            blocked.append("severity_label_conflict")
        elif current_severity != target.severity:
            if severity_owned:
                labels_add.add(target.severity)
                severity_written = True
                if current_severity:
                    labels_remove.add(current_severity)
            else:
                blocked.append("severity_human_override")

        existing_components = existing & self.manifest.component_labels
        target_components = set(target.components)
        components_added = target_components - existing_components
        labels_add.update(components_added)
        if state:
            labels_remove.update((set(state.components) & existing_components) - target_components)
        bot_components = components_added
        if state:
            bot_components |= set(state.components) & existing_components & target_components

        next_state = BotState(
            issue_number=target.issue_number,
            priority=target.priority if priority_written else state_priority(state),
            severity=target.severity if severity_written else state_severity(state),
            components=tuple(sorted(bot_components)),
        )
        return MutationPlan(
            target=target,
            labels_add=tuple(sorted(labels_add)),
            labels_remove=tuple(sorted(labels_remove)),
            blocked=tuple(blocked),
            next_state=next_state,
        )


def target_from_ranked(item: RankedIssue) -> MutationTarget:
    return MutationTarget(
        issue_number=item.issue.number,
        priority=item.result.priority.value,
        severity=f"severity:{item.issue.severity.value}",
        components=item.issue.component_labels,
    )


def state_priority(state: BotState | None) -> str | None:
    return state.priority if state else None


def state_severity(state: BotState | None) -> str | None:
    return state.severity if state else None
