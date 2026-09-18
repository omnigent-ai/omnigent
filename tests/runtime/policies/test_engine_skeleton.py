"""
Tests for the Phase 2 :class:`PolicyEngine` skeleton.

At this phase the engine has no concrete policy subclasses, so
``evaluate`` always returns ALLOW. The tests lock in the
properties the engine MUST already have now because later
phases rely on them:

- ALLOW is the default composed decision with zero policies.
- ``apply_label_writes`` persists via the store and updates
  the hot cache.
- ``spec_for`` resolves by policy name (used in Phase 8 ASK
  routing).
- Empty ``set_labels`` is a no-op (no transaction).
- Hot cache is a defensive copy — callers that mutate what
  they read don't corrupt engine state.

Concrete policy-dispatch tests land in the FunctionPolicy
test modules.
"""

from __future__ import annotations

import pytest

from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import (
    LabelDef,
    Phase,
    PolicyAction,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


@pytest.fixture()
def engine(
    conversation_store: SqlAlchemyConversationStore,
) -> PolicyEngine:
    """
    PolicyEngine bound to a freshly created conversation.

    Zero policies, zero label defs — the Phase 2 default
    shape. Tests that need declared labels or policies build
    their own engine locally.
    """
    conv = conversation_store.create_conversation()
    return PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
    )


# ── evaluate() skeleton behavior ───────────────────────


@pytest.mark.asyncio
async def test_evaluate_allows_with_zero_policies(
    engine: PolicyEngine,
) -> None:
    """An engine with no policies returns ALLOW for every
    phase. If this regresses, the four enforcement sites would
    start blocking every request as soon as the engine is
    wired in.
    """
    ctx = EvaluationContext(
        phase=Phase.REQUEST,
        content="hello",
        tool_name=None,
    )
    result = await engine.evaluate(ctx)
    # Every field is pinned explicitly — a regression that
    # sets `reason` or `set_labels` to something non-None
    # would flip one of these.
    assert result.action == PolicyAction.ALLOW
    assert result.reason is None
    assert result.set_labels is None
    assert result.deciding_policy is None


@pytest.mark.asyncio
async def test_evaluate_allows_for_every_phase(
    engine: PolicyEngine,
) -> None:
    """Iterate through all four phases — every one ALLOWs.
    This is cheap insurance that the Phase 2 no-op never
    accidentally gates a phase differently."""
    for phase in Phase:
        content = "body" if phase in (Phase.REQUEST, Phase.RESPONSE) else {"tool": "x"}
        ctx = EvaluationContext(phase=phase, content=content, tool_name=None)
        result = await engine.evaluate(ctx)
        # Per-phase assertion with phase in the error message
        # so a future regression flags which phase changed
        # semantics — critical for debugging.
        assert result.action == PolicyAction.ALLOW, (
            f"Expected ALLOW for phase {phase.value!r}, got {result.action!r}"
        )


# ── apply_label_writes ─────────────────────────────────


def test_apply_label_writes_persists_and_updates_cache(
    engine: PolicyEngine,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Writes land in the store AND update the in-memory hot
    cache. Missing either is a regression: store-only would
    make subsequent evaluate() calls see stale labels;
    cache-only would lose writes on workflow restart."""
    engine.apply_label_writes({"integrity": "0"})
    # Hot cache: read via the engine's labels property.
    assert engine.labels == {"integrity": "0"}
    # Persisted: round-trip through the store.
    conv = conversation_store.get_conversation(engine.conversation_id)
    assert conv is not None
    assert conv.labels == {"integrity": "0"}


def test_apply_label_writes_empty_dict_is_noop(
    engine: PolicyEngine,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Empty writes must NOT open a transaction. This guards
    against accidental cache reset or an unnecessary DB
    round-trip when the engine evaluates a phase with no
    policy writes."""
    engine.apply_label_writes({"x": "1"})
    # Intentional no-op call — hot cache must not reset.
    engine.apply_label_writes({})
    assert engine.labels == {"x": "1"}
    conv = conversation_store.get_conversation(engine.conversation_id)
    assert conv is not None
    assert conv.labels == {"x": "1"}


def test_apply_label_writes_multi_key_batched(
    engine: PolicyEngine,
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A single call with multiple keys writes them all in
    one store transaction (the store handles the atomicity —
    see Phase 1 tests). Here we verify the engine forwards
    the batch intact rather than iterating per-key."""
    updates = {"integrity": "0", "sensitivity": "confidential"}
    engine.apply_label_writes(updates)
    # Both keys present in both layers.
    assert engine.labels == updates
    conv = conversation_store.get_conversation(engine.conversation_id)
    assert conv is not None
    assert conv.labels == updates


def test_labels_property_returns_defensive_copy(
    engine: PolicyEngine,
) -> None:
    """Mutating the dict returned by `labels` must not leak
    into the engine's internal state. Without the defensive
    copy, a policy or debugger that does
    ``ctx['labels']['x'] = 'y'`` would silently corrupt the
    evaluation state for every subsequent policy in the
    chain."""
    engine.apply_label_writes({"integrity": "1"})
    snapshot = engine.labels
    # Identity check — proves the property returns a fresh
    # dict, not the internal reference. Without this check,
    # a bug that returned the internal dict AND also some
    # other mechanism preventing mutation (e.g. MappingProxy
    # wrapping) would pass the later equality assertion.
    assert snapshot is not engine.labels
    snapshot["integrity"] = "tampered"
    # The engine's own view is unchanged — the returned dict
    # was a copy.
    assert engine.labels == {"integrity": "1"}


# ── spec_for ───────────────────────────────────────────


def test_spec_for_none_returns_none(
    engine: PolicyEngine,
) -> None:
    """None input short-circuits to None — the ASK flow path
    relies on this when the deciding_policy attribute is
    absent (pure-ALLOW compositions)."""
    assert engine.spec_for(None) is None


def test_spec_for_unknown_name_returns_none(
    engine: PolicyEngine,
) -> None:
    """Querying an engine for a policy it doesn't own must
    return None, not raise. The caller (the elicitation
    helper :func:`_await_elicitation`) uses the fallback
    timeout in that case."""
    assert engine.spec_for("does_not_exist") is None


def test_spec_for_finds_policy_by_name(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """When a policy with the given name exists, spec_for
    returns its spec. Proves the YAML-order list lookup works."""
    from omnigent.spec.types import PhaseSelector
    from tests.runtime.policies.conftest import make_fixed_policy

    conv = conversation_store.create_conversation()
    policies = [
        make_fixed_policy(
            name=f"policy_{i}",
            on=[PhaseSelector(phase=Phase.REQUEST)],
            action=PolicyAction.ALLOW,
        )
        for i in range(3)
    ]
    specs = [p.spec for p in policies]
    eng = PolicyEngine(
        policies=policies,
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
    )
    # Middle name to prove we're not just returning the first.
    got = eng.spec_for("policy_1")
    assert got is not None
    # Identity check: same object reference means the list
    # lookup walked through Policy instances and returned
    # each's `.spec` — not a recreated dummy spec.
    assert got is specs[1]


# ── Constructor initialization ─────────────────────────


def test_initial_labels_seeded_into_hot_cache(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """`initial_labels` at construction populate the hot
    cache so the first `evaluate()` call sees them. Without
    this, conditions that gate on pre-existing labels would
    fail silently on the first evaluation of a new engine."""
    conv = conversation_store.create_conversation()
    eng = PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={"integrity": "1"},
        conversation_store=conversation_store,
    )
    # Matches the constructor input exactly.
    assert eng.labels == {"integrity": "1"}


def test_initial_labels_copy_isolated_from_caller(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Mutating the dict passed to the constructor must not
    affect the engine's state. Without dict(initial_labels),
    callers that reuse their seeding dict for something else
    would see the engine spuriously accumulate writes."""
    conv = conversation_store.create_conversation()
    caller_dict = {"integrity": "1"}
    eng = PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels=caller_dict,
        conversation_store=conversation_store,
    )
    caller_dict["sensitivity"] = "public"
    # Caller's later mutation must NOT show up on the engine.
    assert eng.labels == {"integrity": "1"}


# ── label_defs + ask_timeout storage ───────────────────


def test_stores_label_defs_and_timeout(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Non-default `label_defs` and `ask_timeout` are held
    on the engine intact — later phases read these."""
    conv = conversation_store.create_conversation()
    defs = {"integrity": LabelDef(initial="1", values=["0", "1"])}
    eng = PolicyEngine(
        policies=[],
        label_defs=defs,
        ask_timeout=120,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
    )
    assert eng.label_defs is defs
    assert eng.ask_timeout == 120
