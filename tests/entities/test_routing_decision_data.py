"""
Tests for the routing-identity fields on ``RoutingDecisionData``.

The intelligent-routing MVP adds harness / scope / decision identity to
the routing-decision transcript item. Every field is defaulted so rows
persisted before they existed still deserialize.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from omnigent.entities.conversation import RoutingDecisionData, parse_item_data

_LEGACY_ROW = {
    "model": "databricks-claude-opus-4-8",
    "applied": True,
    "rationale": "Multi-file refactor needs deep reasoning.",
}


def test_legacy_row_deserializes_with_defaults() -> None:
    data = parse_item_data("routing_decision", dict(_LEGACY_ROW))
    assert isinstance(data, RoutingDecisionData)
    assert data.model == "databricks-claude-opus-4-8"
    assert data.harness is None
    assert data.scope == "turn"
    assert data.decision_id is None
    assert data.raw_model is None
    assert data.attempted_override is None


@pytest.mark.parametrize(
    ("row", "expected_keys"),
    [
        # Every routing-identity field set: the dump carries each one and
        # rebuilding from it reproduces the model exactly.
        (
            {
                "model": "databricks-gpt-5-6-sol",
                "applied": True,
                "rationale": "Short prompt, cheapest arm.",
                "agent": "claude_code",
                "harness": "codex",
                "scope": "native_subagent",
                "decision_id": "dec_abc123",
                "raw_model": "gpt-5-6-sol",
                "attempted_override": "databricks-gpt-5-5",
            },
            {
                "harness": "codex",
                "scope": "native_subagent",
                "decision_id": "dec_abc123",
                "raw_model": "gpt-5-6-sol",
            },
        ),
        # An unapplied advisory decision: the unset optional stays null in the
        # dump rather than being dropped from the wire.
        (
            {
                "model": "databricks-claude-sonnet-5",
                "applied": False,
                "rationale": "Advise only.",
                "harness": "claude-native",
                "scope": "session",
                "decision_id": "dec_1",
            },
            {
                "harness": "claude-native",
                "scope": "session",
                "decision_id": "dec_1",
                "raw_model": None,
            },
        ),
    ],
)
def test_dump_carries_the_routing_identity_and_round_trips(
    row: dict[str, object], expected_keys: dict[str, object]
) -> None:
    original = RoutingDecisionData(**row)  # type: ignore[arg-type]
    dumped = original.model_dump()
    for key, want in expected_keys.items():
        assert dumped[key] == want, key
    assert RoutingDecisionData(**dumped) == original


@pytest.mark.parametrize("scope", ["session", "turn", "child_session", "native_subagent"])
def test_every_scope_value_validates(scope: str) -> None:
    data = RoutingDecisionData(
        model="databricks-claude-sonnet-5",
        applied=True,
        rationale="ok",
        scope=scope,  # type: ignore[arg-type]
    )
    assert data.scope == scope


def test_unknown_scope_rejected() -> None:
    with pytest.raises(ValidationError):
        RoutingDecisionData(
            model="databricks-claude-sonnet-5",
            applied=True,
            rationale="ok",
            scope="galaxy",  # type: ignore[arg-type]
        )
