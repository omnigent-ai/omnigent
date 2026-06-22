"""
Tests for the built-in session-risk-score policy
(:mod:`omnigent.policies.builtins.risk_score`) — the single configurable
factory ``risk_score_policy``.

Layers:

- **Layer 1** — direct callable: per-call scoring, sensitive-label scoring from
  tool results, threshold gating (ASK/DENY), MCP-prefix-agnostic matching,
  per-actor offsets, escalation-vs-scoring precedence, and
  abstention on non-tool phases.
- **Layer 2** — spec resolution through :func:`resolve_function_policy`.
- **Layer 3** — accumulation through a real :class:`PolicyEngine` + SQLite store:
  proves the score survives an engine rebuild via persisted ``session_state`` and
  that crossing the threshold gates a guarded tool.
- **Layer 4** — registry discovery + factory-param validation.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnigent.policies.builtins.risk_score import (
    DEFAULT_RISK_STATE_KEY,
    risk_score_policy,
)
from omnigent.policies.function import FunctionPolicy, resolve_function_policy
from omnigent.policies.registry import get_registry, load_registry, validate_factory_params
from omnigent.policies.schema import PolicyEvent
from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import FunctionPolicySpec, FunctionRef, Phase, PolicyAction
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.unit.policies.builtins.helpers import tool_call_event as tc
from tests.unit.policies.builtins.helpers import tool_result_event as tr

_HANDLER = "omnigent.policies.builtins.risk_score.risk_score_policy"


def _tc_actor(tool: str, run_as: str, session_state: dict[str, Any] | None = None) -> PolicyEvent:
    """
    Build a ``tool_call`` event carrying an actor identity.

    The shared ``tool_call_event`` helper leaves ``actor`` empty; this variant
    sets ``context.actor.run_as`` so per-actor offset behavior can be tested.

    :param tool: Tool name (set as ``target`` and ``data.name``), e.g.
        ``"gmail_message_send"``.
    :param run_as: Authenticated user email under ``context.actor.run_as``, e.g.
        ``"contractor@example.com"``.
    :param session_state: Optional persisted state. ``None`` means empty.
    :returns: A ``tool_call`` event dict with the actor populated.
    """
    return {
        "type": "tool_call",
        "target": tool,
        "data": {"name": tool, "arguments": {}},
        "context": {"actor": {"run_as": run_as}, "usage": {}},
        "session_state": session_state or {},
    }


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — per-call scoring
# ══════════════════════════════════════════════════════════════════════════════


def test_configured_tool_call_increments_score() -> None:
    """A configured tool call returns ALLOW with the right increment.

    If this breaks (no state_updates / wrong key / wrong delta), the session
    risk never accrues and the gate downstream can never fire.
    """
    result = risk_score_policy(tool_points={"web_search": 10})(tc("web_search", {"q": "x"}))
    assert result is not None
    assert result["result"] == "ALLOW"
    # Exactly one increment of +10 on the default key — proves the configured
    # weight (10) flows into a session_state mutation, not a no-op ALLOW.
    assert result["state_updates"] == [
        {"key": DEFAULT_RISK_STATE_KEY, "action": "increment", "value": 10}
    ]


def test_unconfigured_tool_call_abstains() -> None:
    """A tool with no configured weight abstains (None), adding no risk.

    A non-None result would mean the policy scores tools it was never told about.
    """
    assert risk_score_policy(tool_points={"web_search": 10})(tc("other_tool", {})) is None


@pytest.mark.parametrize(
    "raw_tool",
    ["web_search", "mcp__google__web_search", "mcp__some_server__web_search"],
)
def test_scoring_is_mcp_prefix_agnostic(raw_tool: str) -> None:
    """A configured canonical name matches the tool under any server prefix.

    Proves the ``"__"``-suffix match works for the bare name and arbitrary MCP
    prefixes, so one config covers every server exposing the tool.
    """
    result = risk_score_policy(tool_points={"web_search": 10})(tc(raw_tool, {}))
    assert result is not None and result["state_updates"][0]["value"] == 10


def test_scoring_match_respects_segment_boundary() -> None:
    """A configured name must match a whole ``__``-segment, not a substring.

    ``"search"`` must NOT match ``"web_search"`` — otherwise an over-broad config
    name would silently score unrelated tools.
    """
    assert risk_score_policy(tool_points={"search": 10})(tc("web_search", {})) is None


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — sensitive-label scoring from tool results
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "label", ["Highly Confidential", "highly confidential", "HIGHLY CONFIDENTIAL"]
)
def test_sensitive_label_in_result_increments(label: str) -> None:
    """A result carrying a configured classification adds points, case-insensitively.

    Failure means reading sensitive material doesn't raise risk — the core
    "reading a Highly Confidential doc bumps the score" behavior.
    """
    policy = risk_score_policy(sensitive_labels={"Highly Confidential": 30})
    result = policy(tr("docs_document_get", json.dumps({"label_classification": label})))
    assert result is not None and result["result"] == "ALLOW"
    # +30 = the configured weight for this label; a different value would mean the
    # label→weight lookup or case-folding is wrong.
    assert result["state_updates"][0]["value"] == 30


def test_non_sensitive_label_abstains() -> None:
    """A result with a non-configured classification adds no risk.

    A non-None result would mean every labeled read inflates the score.
    """
    policy = risk_score_policy(sensitive_labels={"Highly Confidential": 30})
    assert (
        policy(tr("docs_document_get", json.dumps({"label_classification": "internal"}))) is None
    )


def test_nested_label_is_found() -> None:
    """A classification nested inside the result payload is still detected.

    Proves the depth-bounded walk descends into nested dicts (real MCP results
    wrap metadata several levels deep).
    """
    policy = risk_score_policy(sensitive_labels={"restricted": 20})
    payload = json.dumps({"file": {"meta": {"classification": "RESTRICTED"}}})
    result = policy(tr("drive_file_get", payload))
    assert result is not None and result["state_updates"][0]["value"] == 20


def test_multiple_labels_takes_max_points() -> None:
    """When several configured labels appear, the highest weight is added once.

    20 (not 50 or 30) proves we take the max single match, not the sum — a result
    that echoes the same classification in two fields must not double-count.
    """
    policy = risk_score_policy(sensitive_labels={"confidential": 30, "secret": 20})
    payload = json.dumps(
        {"a": {"classification": "Confidential"}, "b": {"classification": "Confidential"}}
    )
    result = policy(tr("drive_file_get", payload))
    # Both fields carry "Confidential" (weight 30); max is 30, counted once.
    assert result is not None and result["state_updates"] == [
        {"key": DEFAULT_RISK_STATE_KEY, "action": "increment", "value": 30}
    ]


def test_result_scoring_off_when_no_sensitive_labels() -> None:
    """With no ``sensitive_labels`` configured, results never score.

    Guards the early-out: a pure call-scoring policy must ignore result payloads.
    """
    policy = risk_score_policy(tool_points={"web_search": 10})
    assert (
        policy(tr("docs_document_get", json.dumps({"label_classification": "Top Secret"}))) is None
    )


# ══════════════════════════════════════════════════════════════════════════════
# Layer 1 — threshold gating of guarded tools
# ══════════════════════════════════════════════════════════════════════════════


def test_guarded_tool_below_threshold_abstains() -> None:
    """Below threshold, a guarded tool is not gated (abstains → ALLOW).

    A non-None result would mean the gate fires before enough risk accrued.
    """
    policy = risk_score_policy(threshold=50, guarded_tools=["gmail_message_send"])
    event = tc("gmail_message_send", {}, {DEFAULT_RISK_STATE_KEY: 10})  # 10 < 50
    assert policy(event) is None


def test_guarded_tool_at_threshold_asks() -> None:
    """At/above threshold, a guarded tool escalates to ASK by default.

    The boundary is inclusive (score == threshold gates); the reason embeds the
    score so the user sees why approval is required.
    """
    policy = risk_score_policy(threshold=50, guarded_tools=["gmail_message_send"])
    event = tc("gmail_message_send", {}, {DEFAULT_RISK_STATE_KEY: 50})  # 50 >= 50
    result = policy(event)
    assert result is not None and result["result"] == "ASK"
    assert "50" in result["reason"]  # score surfaced in the approval prompt


def test_guarded_tool_escalate_action_deny() -> None:
    """``escalate_action='DENY'`` hard-blocks over threshold instead of asking."""
    policy = risk_score_policy(
        threshold=50, guarded_tools=["gmail_message_send"], escalate_action="DENY"
    )
    event = tc("gmail_message_send", {}, {DEFAULT_RISK_STATE_KEY: 60})
    result = policy(event)
    assert result is not None and result["result"] == "DENY"


def test_guarded_tool_gating_is_mcp_prefix_agnostic() -> None:
    """A guarded canonical name gates the tool under any server prefix."""
    policy = risk_score_policy(threshold=10, guarded_tools=["gmail_message_send"])
    event = tc("mcp__google__gmail_message_send", {}, {DEFAULT_RISK_STATE_KEY: 10})
    result = policy(event)
    assert result is not None and result["result"] == "ASK"


def test_per_actor_offset_only_affects_that_actor() -> None:
    """``initial_scores_by_actor`` seeds the score for the named actor only.

    The configured contractor gates immediately; a different user (no offset)
    stays under threshold. Proves per-actor seeding keys on ``actor.run_as``.
    """
    policy = risk_score_policy(
        threshold=50,
        initial_scores_by_actor={"contractor@example.com": 50},
        guarded_tools=["gmail_message_send"],
    )
    gated = policy(_tc_actor("gmail_message_send", "contractor@example.com"))
    assert gated is not None and gated["result"] == "ASK"
    # Employee has no offset → effective score 0 < 50 → not gated.
    assert policy(_tc_actor("gmail_message_send", "employee@example.com")) is None


def test_guarded_scorer_scores_below_then_gates_above() -> None:
    """A tool that is both scored and guarded: scores below threshold, gates above.

    Documents the precedence rule — below threshold the tool accrues its weight;
    at/above threshold the escalation replaces scoring (and on ASK the engine
    withholds state-updates anyway).
    """
    policy = risk_score_policy(
        threshold=50,
        tool_points={"gmail_message_send": 5},
        guarded_tools=["gmail_message_send"],
    )
    below = policy(tc("gmail_message_send", {}, {DEFAULT_RISK_STATE_KEY: 0}))
    assert below is not None and below["result"] == "ALLOW"
    assert below["state_updates"][0]["value"] == 5  # accrues while still low-risk
    above = policy(tc("gmail_message_send", {}, {DEFAULT_RISK_STATE_KEY: 50}))
    assert above is not None and above["result"] == "ASK"
    # Escalation carries no state mutation — gating is not a scoring event.
    assert "state_updates" not in above


@pytest.mark.parametrize("phase", ["request", "response"])
def test_abstains_on_non_tool_phases(phase: str) -> None:
    """The policy only acts on tool phases; request/response abstain.

    Function policies see every phase, so the callable must self-select; a
    non-None result here would mean it acts where it has no signal.
    """
    policy = risk_score_policy(
        tool_points={"web_search": 10}, guarded_tools=["gmail_message_send"]
    )
    event: PolicyEvent = {"type": phase, "target": None, "data": "hello", "session_state": {}}
    assert policy(event) is None


def test_invalid_escalate_action_raises() -> None:
    """An unknown ``escalate_action`` fails loud at factory build time.

    Catching this at build (not silently defaulting) means a typo in the spec
    surfaces immediately rather than degrading to an unexpected verdict.
    """
    with pytest.raises(ValueError, match="escalate_action"):
        risk_score_policy(escalate_action="warn")


# ══════════════════════════════════════════════════════════════════════════════
# Layer 2 — resolution through resolve_function_policy
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_resolve_from_spec_gates_over_threshold() -> None:
    """The factory resolves and runs via ``resolve_function_policy``.

    Drives the same gating through the real spec → FunctionPolicy path that the
    server uses, asserting the coerced ``PolicyResult.action`` is ASK.
    """
    spec = FunctionPolicySpec(
        name="risk",
        on=None,
        function=FunctionRef(
            path=_HANDLER,
            arguments={"threshold": 20, "guarded_tools": ["gmail_message_send"]},
        ),
    )
    policy: FunctionPolicy = resolve_function_policy(spec)
    result = await policy.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="gmail_message_send",
            content={"name": "gmail_message_send", "arguments": {}},
            session_state={DEFAULT_RISK_STATE_KEY: 25},  # 25 >= 20
        ),
        {},  # label context (no labels declared)
    )
    assert result.action == PolicyAction.ASK


# ══════════════════════════════════════════════════════════════════════════════
# Layer 3 — accumulation through a real PolicyEngine + SQLite store
# ══════════════════════════════════════════════════════════════════════════════


@pytest.fixture()
def conversation_store(db_uri: str) -> SqlAlchemyConversationStore:
    """
    Conversation store backed by a per-test SQLite DB.

    :param db_uri: Root-conftest fixture providing a migrated SQLite URI.
    :returns: A real store for exercising session_state persistence.
    """
    return SqlAlchemyConversationStore(db_uri)


def _engine(
    store: SqlAlchemyConversationStore,
    conv_id: str,
    state: dict[str, Any],
    arguments: dict[str, Any],
) -> PolicyEngine:
    """
    Build a fresh :class:`PolicyEngine` over a single risk_score policy.

    Mirrors the per-evaluation rebuild ``build_policy_engine`` does in the server,
    so each call re-derives the policy from persisted state (no closure carryover).

    :param store: Backing conversation store.
    :param conv_id: Conversation to bind to.
    :param state: Seed ``session_state``.
    :param arguments: Factory kwargs (non-empty so the factory is invoked).
    :returns: A ready engine.
    """
    spec = FunctionPolicySpec(
        name="risk",
        on=None,
        function=FunctionRef(path=_HANDLER, arguments=arguments),
    )
    return PolicyEngine(
        policies=[resolve_function_policy(spec)],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv_id,
        initial_labels={},
        initial_session_state=state,
        conversation_store=store,
    )


@pytest.mark.asyncio
async def test_score_accumulates_across_rebuilds_then_gates(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Risk accrued in earlier turns persists and eventually gates a guarded tool.

    A new engine is built per turn (as the server does), so this only passes if
    the score round-trips through the store rather than living in closure state.
    Uses ``escalate_action='DENY'`` so the gate resolves synchronously without
    the ASK approval round-trip.
    """
    args = {
        "threshold": 20,
        "tool_points": {"web_search": 10},
        "guarded_tools": ["gmail_message_send"],
        "escalate_action": "DENY",
    }
    conv = conversation_store.create_conversation()

    # Turn 1: one web_search → +10. Guarded tool still allowed (10 < 20).
    engine1 = _engine(conversation_store, conv.id, {}, args)
    r1 = await engine1.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="web_search",
            content={"name": "web_search", "arguments": {"q": "x"}},
        )
    )
    assert r1.action == PolicyAction.ALLOW
    reloaded = conversation_store.get_conversation(conv.id)
    assert reloaded is not None
    # +10 persisted — if 0/missing, the increment never reached the store.
    assert reloaded.session_state.get(DEFAULT_RISK_STATE_KEY) == 10

    not_yet = await engine1.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="gmail_message_send",
            content={"name": "gmail_message_send", "arguments": {}},
        )
    )
    assert not_yet.action == PolicyAction.ALLOW  # 10 < 20, not gated

    # Turn 2: a fresh engine seeded from the persisted state. Another +10 → 20.
    engine2 = _engine(conversation_store, conv.id, dict(reloaded.session_state), args)
    r2 = await engine2.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="web_search",
            content={"name": "web_search", "arguments": {"q": "y"}},
        )
    )
    assert r2.action == PolicyAction.ALLOW
    reloaded2 = conversation_store.get_conversation(conv.id)
    assert reloaded2 is not None
    # 20 = 10 (turn 1) + 10 (turn 2). If 10, the seed state was dropped on rebuild.
    assert reloaded2.session_state.get(DEFAULT_RISK_STATE_KEY) == 20

    # Turn 3: now at threshold → the guarded tool is blocked.
    engine3 = _engine(conversation_store, conv.id, dict(reloaded2.session_state), args)
    gated = await engine3.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="gmail_message_send",
            content={"name": "gmail_message_send", "arguments": {}},
        )
    )
    # DENY only because the accumulated score (20) reached the threshold.
    assert gated.action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_sensitive_result_accrues_risk_through_engine(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Reading a sensitive-labeled result raises risk enough to gate via the engine.

    Exercises the full tool_result → label scoring → persisted state → gate path
    end-to-end through a real engine and store.
    """
    args = {
        "threshold": 25,
        "sensitive_labels": {"Highly Confidential": 30},
        "guarded_tools": ["gmail_message_send"],
        "escalate_action": "DENY",
    }
    conv = conversation_store.create_conversation()

    engine1 = _engine(conversation_store, conv.id, {}, args)
    read = await engine1.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_RESULT,
            tool_name="docs_document_get",
            content={"result": json.dumps({"label_classification": "Highly Confidential"})},
        )
    )
    assert read.action == PolicyAction.ALLOW
    reloaded = conversation_store.get_conversation(conv.id)
    assert reloaded is not None
    # +30 from the confidential read — the DLP label drove the increment.
    assert reloaded.session_state.get(DEFAULT_RISK_STATE_KEY) == 30

    engine2 = _engine(conversation_store, conv.id, dict(reloaded.session_state), args)
    gated = await engine2.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="gmail_message_send",
            content={"name": "gmail_message_send", "arguments": {}},
        )
    )
    # 30 >= 25 → the send is blocked after the confidential read.
    assert gated.action == PolicyAction.DENY


# ══════════════════════════════════════════════════════════════════════════════
# Layer 4 — registry discovery + param validation
# ══════════════════════════════════════════════════════════════════════════════


def test_registry_discovers_risk_score() -> None:
    """The policy is discoverable as a factory entry with a params schema.

    Failure means it isn't browsable via GET /v1/policy-registry and its params
    won't be validated on attach.
    """
    load_registry()
    by_handler = {e.handler: e for e in get_registry()}
    assert _HANDLER in by_handler
    assert by_handler[_HANDLER].kind == "factory"
    assert by_handler[_HANDLER].params_schema is not None


def test_registry_validates_factory_params() -> None:
    """The schema accepts valid params and rejects unknown keys / wrong types."""
    load_registry()
    good = {"threshold": 50, "guarded_tools": ["gmail_message_send"], "escalate_action": "ASK"}
    assert validate_factory_params(_HANDLER, good) is None
    err_unknown = validate_factory_params(_HANDLER, {"bogus": 1})
    assert err_unknown is not None and "bogus" in err_unknown
    assert validate_factory_params(_HANDLER, {"threshold": "high"}) is not None
