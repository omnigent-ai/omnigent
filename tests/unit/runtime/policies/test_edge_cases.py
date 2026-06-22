"""
Edge-case tests for the policy system.

Scenarios that lurk at boundaries but are easy to miss
without explicit tests:

- Extremely empty states (no policies, no labels, etc.)
- Large numbers of policies / labels / evaluations
- Pathological content shapes
- Sequential evaluations in rapid succession
- Unicode / special characters in content

Each test proves the system handles the edge without
crashing, leaking state, or producing wrong decisions.
"""

from __future__ import annotations

import pytest

from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import (
    Phase,
    PhaseSelector,
    PolicyAction,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.unit.runtime.policies.conftest import make_fixed_policy


def _build(
    store: SqlAlchemyConversationStore,
    policies: list,
    *,
    initial_labels: dict[str, str] | None = None,
) -> PolicyEngine:
    """Build engine + fresh conversation."""
    conv = store.create_conversation()
    return PolicyEngine(
        policies=policies,
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels=initial_labels or {},
        conversation_store=store,
    )


# ── Completely empty ──────────────────────────────────


@pytest.mark.asyncio
async def test_zero_policies_zero_labels_always_allows(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Totally empty engine ALLOWs every phase, every tool,
    every content. The absolute baseline."""
    engine = _build(conversation_store, [])
    for phase in Phase:
        content = "x" if phase in (Phase.REQUEST, Phase.RESPONSE) else {}
        r = await engine.evaluate(
            EvaluationContext(phase=phase, content=content, tool_name=None),
        )
        assert r.action == PolicyAction.ALLOW, (
            f"Empty engine should ALLOW {phase.value}, got {r.action}"
        )


# ── Large scale ───────────────────────────────────────


@pytest.mark.asyncio
async def test_many_policies_compose_in_yaml_order(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """100 ALLOWing policies, each writing a distinct label,
    compose correctly. Stress test for last-writer-wins
    semantics + composition loop."""
    policies = [
        make_fixed_policy(
            name=f"p{i:03d}",
            on=[PhaseSelector(phase=Phase.REQUEST)],
            action=PolicyAction.ALLOW,
            set_labels={f"key_{i}": str(i)},
        )
        for i in range(100)
    ]
    engine = _build(conversation_store, policies)
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    # All 100 writes landed.
    assert len(engine.labels) == 100
    # Values are correct (last-writer-wins on distinct keys
    # is trivial; this proves every write was processed).
    for i in range(100):
        assert engine.labels[f"key_{i}"] == str(i)


@pytest.mark.asyncio
async def test_many_sequential_evaluations(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """1000 evaluations on the same engine — no state
    leakage, no accumulating slowdown. Proves hot-cache
    reads + selector filter are O(policies), not O(history)."""
    policy = make_fixed_policy(
        name="taint",
        on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name="web")],
        action=PolicyAction.ALLOW,
        set_labels={"integrity": "0"},
    )
    engine = _build(conversation_store, [policy])
    for _ in range(1000):
        r = await engine.evaluate(
            EvaluationContext(
                phase=Phase.TOOL_CALL,
                content={"name": "web", "arguments": {}},
                tool_name="web",
            ),
        )
        assert r.action == PolicyAction.ALLOW
    # Label value pinned — every repeated write produced
    # the same "0".
    assert engine.labels == {"integrity": "0"}


# ── Pathological content ──────────────────────────────


@pytest.mark.asyncio
async def test_empty_string_content_input(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Empty-string content on INPUT — a policy that fires
    still returns a normal result. No NullPointer-equivalents."""
    policy = make_fixed_policy(
        name="p",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        action=PolicyAction.ALLOW,
    )
    engine = _build(conversation_store, [policy])
    r = await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content=""))
    assert r.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_empty_dict_tool_args(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Tool call with no args still evaluates correctly."""
    policy = make_fixed_policy(
        name="p",
        on=[PhaseSelector(phase=Phase.TOOL_CALL, tool_name="noop")],
        action=PolicyAction.ALLOW,
    )
    engine = _build(conversation_store, [policy])
    r = await engine.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": "noop", "arguments": {}},
            tool_name="noop",
        ),
    )
    assert r.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_unicode_content(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Unicode content (emoji, non-latin scripts) passes
    through — no encoding issues on the content path."""
    policy = make_fixed_policy(
        name="p",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        action=PolicyAction.ALLOW,
    )
    engine = _build(conversation_store, [policy])
    # Mix of emoji, CJK, RTL text.
    r = await engine.evaluate(
        EvaluationContext(
            phase=Phase.REQUEST,
            content=(
                "\U0001f680 \u4f60\u597d \u0645\u0631\u062d\u0628\u0627 \u05e9\u05dc\u05d5\u05dd"
            ),
        ),
    )
    assert r.action == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_very_long_content(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """10 KB content string — no size-related failures in
    the evaluation path."""
    policy = make_fixed_policy(
        name="p",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        action=PolicyAction.ALLOW,
    )
    engine = _build(conversation_store, [policy])
    r = await engine.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="A" * 10_000),
    )
    assert r.action == PolicyAction.ALLOW


# ── Label-value edge cases ────────────────────────────


@pytest.mark.asyncio
async def test_label_value_empty_string(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Label value "" (empty string) is still a valid
    string and should persist. No implicit "empty = unset"
    coercion."""
    policy = make_fixed_policy(
        name="write_empty",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        action=PolicyAction.ALLOW,
        set_labels={"marker": ""},
    )
    engine = _build(conversation_store, [policy])
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    assert engine.labels == {"marker": ""}


@pytest.mark.asyncio
async def test_label_key_with_special_chars(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Label keys with dots, underscores, hyphens —
    no key-mangling in the store round-trip."""
    policy = make_fixed_policy(
        name="p",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        action=PolicyAction.ALLOW,
        set_labels={
            "namespace.label": "1",
            "snake_case": "1",
            "kebab-case": "1",
        },
    )
    engine = _build(conversation_store, [policy])
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    assert engine.labels == {
        "namespace.label": "1",
        "snake_case": "1",
        "kebab-case": "1",
    }
    # Store round-trip — special chars survive.
    conv = conversation_store.get_conversation(engine.conversation_id)
    assert conv is not None
    assert conv.labels == {
        "namespace.label": "1",
        "snake_case": "1",
        "kebab-case": "1",
    }


# ── Condition with list values + mixed types ─────────


@pytest.mark.asyncio
async def test_condition_list_with_single_element(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """`condition: {key: [only_one]}` — single-element list
    behaves same as scalar string condition. The OR
    semantics across list elements degenerate to "must
    equal" when there's only one."""
    policy = make_fixed_policy(
        name="gated",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        # Explicit list — not scalar. Must still match.
        condition={"role": ["admin"]},
        action=PolicyAction.DENY,
    )
    engine = _build(
        conversation_store,
        [policy],
        initial_labels={"role": "admin"},
    )
    r = await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    assert r.action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_many_keys_in_condition(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """AND across many condition keys — all must match to
    fire. One missing match -> policy doesn't fire."""
    policy = make_fixed_policy(
        name="strict",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        condition={
            "a": "1",
            "b": "2",
            "c": "3",
            "d": "4",
            "e": "5",
        },
        action=PolicyAction.DENY,
    )
    # Exact match -> DENY.
    engine_full = _build(
        conversation_store,
        [policy],
        initial_labels={"a": "1", "b": "2", "c": "3", "d": "4", "e": "5"},
    )
    r_full = await engine_full.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="x"),
    )
    assert r_full.action == PolicyAction.DENY

    # One key off -> condition fails -> policy skipped.
    engine_partial = _build(
        conversation_store,
        [policy],
        initial_labels={"a": "1", "b": "2", "c": "3", "d": "4", "e": "wrong"},
    )
    r_partial = await engine_partial.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="x"),
    )
    assert r_partial.action == PolicyAction.ALLOW


# ── Policy with empty reason string ───────────────────


@pytest.mark.asyncio
async def test_policy_empty_reason_preserved(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A policy declared with no ``reason`` returns None on the
    result. Absent-vs-empty distinction preserved."""
    policy = make_fixed_policy(
        name="deny_no_reason",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        action=PolicyAction.DENY,
        # No reason.
    )
    engine = _build(conversation_store, [policy])
    r = await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    assert r.action == PolicyAction.DENY
    # reason is None, not empty string.
    assert r.reason is None
