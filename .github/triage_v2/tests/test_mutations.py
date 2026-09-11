from __future__ import annotations

from issue_prioritization.labels import LabelDefinition, LabelManifest
from issue_prioritization.mutations import BotState, MutationPlanner, MutationTarget


class FakeStates:
    def __init__(self, values=None):
        self.values = values or {}
        self.updated = []

    def load(self):
        return self.values

    def upsert(self, states):
        self.updated.extend(states)


class FakeLegacyPriorities:
    def __init__(self, owned=True):
        self.owned = owned

    def is_bot_owned(self, issue_number, priority):
        return self.owned


def _manifest() -> LabelManifest:
    return LabelManifest(
        labels=(
            LabelDefinition("comp:db", "000000", ""),
            LabelDefinition("comp:server", "000000", ""),
        )
    )


def _target() -> MutationTarget:
    return MutationTarget(1, "P1-high", ("comp:db",))


def test_existing_priority_without_bot_state_is_human_owned() -> None:
    planner = MutationPlanner(_manifest(), FakeStates())

    plan = planner.plan_one(_target(), ("P2-medium",), None)

    assert plan.blocked == ("priority_human_override",)
    assert plan.labels_add == ("comp:db",)
    assert plan.labels_remove == ()
    assert plan.next_state == BotState(1, None, ("comp:db",))


def test_matching_human_labels_do_not_become_bot_owned() -> None:
    planner = MutationPlanner(_manifest(), FakeStates())

    plan = planner.plan_one(_target(), ("P1-high", "comp:db"), None)

    assert plan.labels_add == ()
    assert plan.labels_remove == ()
    assert plan.next_state == BotState(1, None, ())


def test_bot_owned_priority_can_be_regraded() -> None:
    state = BotState(1, "P2-medium", ("comp:server",))
    planner = MutationPlanner(_manifest(), FakeStates({1: state}))

    plan = planner.plan_one(
        _target(),
        ("P2-medium", "severity:S2", "comp:server"),
        state,
    )

    assert plan.blocked == ()
    assert set(plan.labels_add) == {"P1-high", "comp:db"}
    assert set(plan.labels_remove) == {"P2-medium", "severity:S2", "comp:server"}
    assert plan.next_state.priority == "P1-high"


def test_human_priority_change_is_never_overwritten() -> None:
    state = BotState(1, "P0-critical", ("comp:db",))
    planner = MutationPlanner(_manifest(), FakeStates({1: state}))

    plan = planner.plan_one(_target(), ("P3-low", "comp:db"), state)

    assert plan.blocked == ("priority_human_override",)
    assert plan.next_state.priority == "P0-critical"
    assert "P1-high" not in plan.labels_add


def test_human_priority_removal_is_never_undone() -> None:
    state = BotState(1, "P1-high", ("comp:db",))
    planner = MutationPlanner(_manifest(), FakeStates({1: state}))

    plan = planner.plan_one(_target(), ("comp:db",), state)

    assert plan.blocked == ("priority_human_override",)
    assert "P1-high" not in plan.labels_add
    assert plan.next_state.priority == "P1-high"


def test_human_component_labels_are_not_removed() -> None:
    state = BotState(1, None, ("comp:server",))
    planner = MutationPlanner(_manifest(), FakeStates({1: state}))

    plan = planner.plan_one(_target(), ("comp:server", "comp:db"), state)

    assert plan.labels_remove == ("comp:server",)
    assert "comp:db" not in plan.labels_remove
    assert plan.next_state.components == ()


def test_existing_bot_owned_component_stays_owned() -> None:
    state = BotState(1, None, ("comp:db",))
    planner = MutationPlanner(_manifest(), FakeStates({1: state}))

    plan = planner.plan_one(_target(), ("comp:db",), state)

    assert plan.next_state.components == ("comp:db",)


def test_human_removed_bot_component_is_not_readded() -> None:
    state = BotState(1, None, ("comp:db",))
    planner = MutationPlanner(_manifest(), FakeStates({1: state}))

    plan = planner.plan_one(_target(), (), state)

    assert plan.labels_add == ("P1-high",)
    assert plan.labels_remove == ()
    assert plan.blocked == ("component_human_override:comp:db",)
    assert plan.next_state.components == ("comp:db",)


def test_retired_severity_labels_are_always_removed() -> None:
    planner = MutationPlanner(_manifest(), FakeStates())

    plan = planner.plan_one(
        _target(),
        ("P1-high", "severity:S1", "severity:S2", "severity:S3"),
        None,
    )

    assert plan.labels_add == ("comp:db",)
    assert plan.labels_remove == ("severity:S1", "severity:S2", "severity:S3")
    assert plan.blocked == ()


def test_conflicting_priority_labels_are_never_mutated() -> None:
    planner = MutationPlanner(_manifest(), FakeStates())

    plan = planner.plan_one(
        _target(),
        ("P1-high", "P2-medium", "severity:S1"),
        None,
    )

    assert plan.labels_add == ("comp:db",)
    assert plan.labels_remove == ("severity:S1",)
    assert plan.blocked == ("priority_label_conflict",)


def test_legacy_bot_priority_can_be_adopted_for_backfill() -> None:
    planner = MutationPlanner(_manifest(), FakeStates(), FakeLegacyPriorities())

    state = planner.resolve_state(1, ("P2-medium",), None)

    assert state == BotState(1, "P2-medium", ())


def test_legacy_human_priority_is_not_adopted() -> None:
    planner = MutationPlanner(
        _manifest(),
        FakeStates(),
        FakeLegacyPriorities(owned=False),
    )

    assert planner.resolve_state(1, ("P2-medium",), None) is None


def test_classification_corrects_type_and_requests_missing_information() -> None:
    planner = MutationPlanner(_manifest(), FakeStates())
    target = MutationTarget(1, "P1-high", ("comp:db",), "Bug", True)

    plan = planner.plan_one(target, ("Feature",), None)

    assert set(plan.labels_add) == {"Bug", "P1-high", "comp:db", "needs-info"}
    assert plan.labels_remove == ("Feature",)


def test_sufficient_reassessment_removes_needs_info() -> None:
    planner = MutationPlanner(_manifest(), FakeStates())
    target = MutationTarget(1, "P1-high", ("comp:db",), "Bug", False)

    plan = planner.plan_one(target, ("Bug", "needs-info"), None)

    assert "needs-info" in plan.labels_remove


def test_security_issue_is_exempt_from_needs_info_lifecycle() -> None:
    planner = MutationPlanner(_manifest(), FakeStates())
    target = MutationTarget(1, "P1-high", ("comp:db",), "Bug", True)

    plan = planner.plan_one(target, ("Bug", "security"), None)

    assert "needs-info" not in plan.labels_add
    assert "needs_info_security_exempt" in plan.blocked


def test_duplicate_is_exempt_and_drops_stale_needs_info() -> None:
    planner = MutationPlanner(_manifest(), FakeStates())
    target = MutationTarget(1, "P1-high", ("comp:db",), "Bug", True)

    plan = planner.plan_one(target, ("Bug", "duplicate", "needs-info"), None)

    assert "needs-info" in plan.labels_remove
    assert "needs_info_duplicate_exempt" in plan.blocked
