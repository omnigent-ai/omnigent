"""The child-create family gate reads the parent's routing class.

Confinement is a pinned-Smart-Routing feature: the runner's ``sys_agent_list``
only hides out-of-family agents from a parent whose routing class is routed and
pinned. The create gate has to answer the same question, or a session with only
the subagent-routing switch flipped is listed a codex agent and then refused a
400 when it tries to spawn it.
"""

from __future__ import annotations

import pytest

from omnigent.entities.conversation import Conversation
from omnigent.errors import OmnigentError
from omnigent.runner.subagent_routing import (
    AUTO_HARNESS_LABEL_KEY,
    routing_class_from_snapshot,
)
from omnigent.runner.turn_routing import out_of_parent_family
from omnigent.server.routes._sessions.orchestration import (
    _pinned_spawn_family,
    _reject_out_of_family_child,
)


def _conv(
    *,
    cost_control_mode_override: str | None = None,
    subagent_routing_override: str | None = None,
    harness_override: str | None = "codex-native",
    labels: dict[str, str] | None = None,
) -> Conversation:
    return Conversation(
        id="conv_parent",
        created_at=0,
        updated_at=0,
        root_conversation_id="conv_parent",
        labels=labels or {},
        cost_control_mode_override=cost_control_mode_override,
        subagent_routing_override=subagent_routing_override,
        harness_override=harness_override,
    )


_PLAIN = _conv()
_TOGGLE_ONLY = _conv(subagent_routing_override="on")
_PINNED_ROUTED = _conv(cost_control_mode_override="on", subagent_routing_override="on")
_PINNED_ROUTED_SWITCH_OFF = _conv(cost_control_mode_override="on")
_AUTO_ROUTED = _conv(
    cost_control_mode_override="on",
    subagent_routing_override="on",
    harness_override="auto",
    labels={AUTO_HARNESS_LABEL_KEY: "1"},
)


@pytest.mark.parametrize(
    ("parent", "expected"),
    [
        (None, None),
        (_PLAIN, None),
        (_TOGGLE_ONLY, None),
        (_PINNED_ROUTED, "gpt"),
        (_PINNED_ROUTED_SWITCH_OFF, None),
        (_AUTO_ROUTED, None),
    ],
)
def test_confinement_matches_the_routing_class(
    parent: Conversation | None, expected: str | None
) -> None:
    assert _pinned_spawn_family(parent) == expected


@pytest.mark.parametrize(
    "parent", [None, _PLAIN, _TOGGLE_ONLY, _PINNED_ROUTED_SWITCH_OFF, _AUTO_ROUTED]
)
def test_unconfined_parent_creates_across_families(parent: Conversation | None) -> None:
    _reject_out_of_family_child(parent, "claude-native")


def test_pinned_routed_parent_refuses_an_out_of_family_child() -> None:
    with pytest.raises(OmnigentError, match="own model family"):
        _reject_out_of_family_child(_PINNED_ROUTED, "claude-native")


def test_pinned_routed_parent_allows_an_in_family_child() -> None:
    _reject_out_of_family_child(_PINNED_ROUTED, "codex-native")


def test_unresolvable_child_harness_is_allowed() -> None:
    _reject_out_of_family_child(_PINNED_ROUTED, None)


@pytest.mark.parametrize(
    ("parent", "confined"),
    [
        (_PLAIN, False),
        (_TOGGLE_ONLY, False),
        (_PINNED_ROUTED, True),
        (_PINNED_ROUTED_SWITCH_OFF, False),
        (_AUTO_ROUTED, False),
    ],
)
def test_create_gate_agrees_with_the_listing_predicate(
    parent: Conversation, confined: bool
) -> None:
    # The runner hides out-of-family agents on exactly this predicate, so the
    # two halves have to answer alike for every parent.
    routing_class = routing_class_from_snapshot(
        cost_control_mode=parent.cost_control_mode_override,
        harness_override=parent.harness_override,
        labels=parent.labels,
    )
    listing_confines = (
        routing_class.routing_enabled
        and not routing_class.auto_harness
        and parent.subagent_routing_override == "on"
    )
    assert listing_confines is confined
    assert (_pinned_spawn_family(parent) is not None) is confined


@pytest.mark.parametrize(
    ("parent", "expected"),
    [
        (_PLAIN, False),
        (_TOGGLE_ONLY, False),
        (_PINNED_ROUTED, True),
        (_PINNED_ROUTED_SWITCH_OFF, False),
        (_AUTO_ROUTED, False),
    ],
)
def test_turn_family_guard_follows_the_same_predicate(
    parent: Conversation, expected: bool
) -> None:
    child = _conv(harness_override="claude-native")
    assert out_of_parent_family(child, parent, "codex-native", "claude-native") is expected
