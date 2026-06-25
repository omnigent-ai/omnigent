"""Tests for :mod:`omnigent.cost_plan` — the advisor v3 verdict contract.

Covers the single-verdict label round-trip (serialize → parse), the
fail-loud paths for malformed v3 labels, the tolerated legacy-v2 label
(parses to ``None`` rather than crashing the reader), tier ranking, the
reserved-namespace helper, and the one-line ``describe_verdict`` summary.
"""

from __future__ import annotations

import json

import pytest

from omnigent.cost_plan import (
    COST_CONTROL_PLAN_LABEL,
    PLAN_VERSION,
    TIER_ORDER,
    AdvisorVerdict,
    describe_verdict,
    parse_verdict,
    reserved_cost_control_keys,
    tier_rank,
    verdict_to_label_value,
)

_ANCHOR = "2026-06-10T00:00:00+00:00"


def _verdict(
    *,
    tier: str = "expensive",
    model: str = "databricks-claude-opus-4-8",
    applied: bool = True,
) -> AdvisorVerdict:
    """Build a verdict with test defaults.

    :param tier: Difficulty tier, default ``"expensive"``.
    :param model: Brain model id, default the opus tier model.
    :param applied: Whether the brain ran on *model*, default ``True``.
    :returns: A verdict anchored at a fixed test timestamp.
    """
    return AdvisorVerdict(
        tier=tier, model=model, applied=applied, rationale="hard refactor", turn_anchor=_ANCHOR
    )


# ── Tier ordering ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "tier,rank",
    [("cheap", 0), ("medium", 1), ("expensive", 2)],
)
def test_tier_rank_orders_cheap_to_expensive(tier: str, rank: int) -> None:
    """Each tier ranks by its position in TIER_ORDER (cheap < expensive)."""
    # Proves cost ordering is monotonic; a wrong index would let the
    # judge's tier sizing rank backwards.
    assert tier_rank(tier) == rank


def test_tier_rank_unknown_fails_loud() -> None:
    """An unknown tier name raises rather than ranking as cheapest."""
    with pytest.raises(ValueError, match="unknown tier"):
        tier_rank("platinum")


def test_tier_order_is_cheap_medium_expensive() -> None:
    """The contract's three tiers in ascending cost order."""
    assert TIER_ORDER == ("cheap", "medium", "expensive")


# ── Reserved namespace helper ──────────────────────────────────────────────────


def test_reserved_cost_control_keys_filters_namespace() -> None:
    """Only ``cost_control.*`` keys are reported, in mapping order."""
    keys = reserved_cost_control_keys(
        {"cost_control.plan": "{}", "team": "ml", "cost_control.other": "x"}
    )
    # Both reserved keys, the unrelated "team" key dropped — proves the
    # server's reserved-label gate sees exactly the advisor namespace.
    assert keys == ("cost_control.plan", "cost_control.other")


def test_reserved_cost_control_keys_empty_when_none() -> None:
    """No reserved keys => empty tuple (the gate is a no-op)."""
    assert reserved_cost_control_keys({"team": "ml"}) == ()


# ── Round-trip ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("applied", [True, False])
def test_verdict_label_round_trip(applied: bool) -> None:
    """A verdict serializes and parses back identically, applied flag intact."""
    verdict = _verdict(tier="medium", model="databricks-claude-sonnet-4-6", applied=applied)
    raw = verdict_to_label_value(verdict)
    parsed = parse_verdict({COST_CONTROL_PLAN_LABEL: raw})
    # Full-field equality proves every field (incl. the applied bool that
    # the UI keys "shadow vs applied" on) survives the JSON round-trip.
    assert parsed == verdict
    assert parsed is not None
    assert parsed.applied is applied


def test_label_value_is_versioned_compact_json() -> None:
    """The serialized label carries version 3 and the verdict fields."""
    raw = verdict_to_label_value(_verdict(tier="cheap", model="databricks-claude-haiku-4-5"))
    payload = json.loads(raw)
    # version==3 lets readers version-gate; the wrong version would make
    # parse_verdict ignore a real verdict as if it were legacy.
    assert payload["version"] == PLAN_VERSION == 3
    assert payload["tier"] == "cheap"
    assert payload["model"] == "databricks-claude-haiku-4-5"
    assert payload["applied"] is True


def test_parse_verdict_absent_label_is_none() -> None:
    """No label => None (no advised turn yet), not an error."""
    assert parse_verdict({"team": "ml"}) is None


# ── Legacy tolerance ───────────────────────────────────────────────────────────


def test_parse_verdict_tolerates_legacy_v2_label() -> None:
    """A v2 (tier-partition) label in an old session parses to None, not a crash.

    Regression guard: a session created before v3 carries a v2 ``entries``
    label; the reader must keep loading (returning None) instead of raising
    and breaking the whole session view.
    """
    legacy_v2 = json.dumps(
        {
            "version": 2,
            "entries": [{"task": "x", "tier": "cheap", "model": "m"}],
            "rationale": "old",
            "turn_anchor": _ANCHOR,
        }
    )
    assert parse_verdict({COST_CONTROL_PLAN_LABEL: legacy_v2}) is None


def test_parse_verdict_tolerates_legacy_v1_label() -> None:
    """A v1 label likewise parses to None (only the current schema is read)."""
    legacy_v1 = json.dumps({"version": 1, "anything": "goes"})
    assert parse_verdict({COST_CONTROL_PLAN_LABEL: legacy_v1}) is None


# ── Fail-loud on a corrupt v3 label ─────────────────────────────────────────────


def test_parse_verdict_invalid_json_raises() -> None:
    """A non-JSON label is a real corruption and fails loud."""
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_verdict({COST_CONTROL_PLAN_LABEL: "{not json"})


def test_parse_verdict_non_object_root_raises() -> None:
    """A JSON array root is not a verdict object."""
    with pytest.raises(ValueError, match="must be a JSON object"):
        parse_verdict({COST_CONTROL_PLAN_LABEL: "[1, 2, 3]"})


def test_parse_verdict_unknown_tier_raises() -> None:
    """A v3 label naming an unknown tier is corrupt (current schema)."""
    raw = json.dumps(
        {
            "version": 3,
            "tier": "platinum",
            "model": "m",
            "applied": True,
            "rationale": "r",
            "turn_anchor": _ANCHOR,
        }
    )
    with pytest.raises(ValueError, match="expected one of"):
        parse_verdict({COST_CONTROL_PLAN_LABEL: raw})


def test_parse_verdict_missing_model_raises() -> None:
    """A v3 label without a model string is malformed."""
    raw = json.dumps(
        {
            "version": 3,
            "tier": "cheap",
            "model": "",
            "applied": True,
            "rationale": "r",
            "turn_anchor": _ANCHOR,
        }
    )
    with pytest.raises(ValueError, match="non-empty string model"):
        parse_verdict({COST_CONTROL_PLAN_LABEL: raw})


def test_parse_verdict_non_bool_applied_raises() -> None:
    """``applied`` must be a real bool, not a truthy stand-in.

    A string "true" would make the UI's shadow-vs-applied distinction
    silently wrong, so it fails loud.
    """
    raw = json.dumps(
        {
            "version": 3,
            "tier": "cheap",
            "model": "databricks-claude-haiku-4-5",
            "applied": "true",
            "rationale": "r",
            "turn_anchor": _ANCHOR,
        }
    )
    with pytest.raises(ValueError, match="boolean applied field"):
        parse_verdict({COST_CONTROL_PLAN_LABEL: raw})


# ── describe_verdict ────────────────────────────────────────────────────────────


def test_describe_verdict_is_model_and_tier() -> None:
    """The one-line summary names the model and its tier."""
    text = describe_verdict(_verdict(tier="expensive", model="databricks-claude-opus-4-8"))
    assert text == "databricks-claude-opus-4-8 (expensive)"


def test_label_value_caps_long_rationale_at_column_limit() -> None:
    """An oversized judge rationale must trim to fit the varchar(256)
    labels column — Postgres rejects the whole write otherwise (the
    haiku-shows/opus-doesn't bug)."""
    verdict = AdvisorVerdict(
        tier="expensive",
        model="databricks-claude-opus-4-8",
        applied=True,
        rationale="x" * 600,
        turn_anchor="2026-06-11T05:30:45.670436+00:00",
    )
    value = verdict_to_label_value(verdict)
    assert len(value) <= 256
    parsed = parse_verdict({COST_CONTROL_PLAN_LABEL: value})
    assert parsed is not None
    assert parsed.model == verdict.model
    assert parsed.rationale is not None and parsed.rationale.endswith("...")
