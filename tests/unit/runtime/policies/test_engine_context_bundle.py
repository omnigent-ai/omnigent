"""
Tests for the V0 event dict that FunctionPolicy callables receive.

With the V0 Service Policy interface, callables receive an ``event``
dict (and optionally a ``config`` dict) instead of the old
``(ctx, context)`` pair. Verifies:

- Event dict carries the expected V0 shape (type, target, data,
  context).
- The engine's label hot cache is exposed read-only to callables via
  ``event["context"]["labels"]`` (the advisor cost-plan guard gates on
  it), and mutating the exposed copy never corrupts engine state.
- Engine labels still accumulate correctly across evaluations
  (verified via ``engine.labels``).
- Policies see each other's set_labels writes via the engine's
  ``apply_label_writes`` (sequential across evaluations).
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.policies.function import FunctionPolicy
from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import (
    FunctionPolicySpec,
    FunctionRef,
    Phase,
    PhaseSelector,
    PolicyAction,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from tests.unit.runtime.policies.conftest import make_fixed_policy


def _capturing_policy(bucket: dict[str, Any]) -> FunctionPolicy:
    """Build a FunctionPolicy that records the V0 event it
    receives into *bucket*. Used to inspect what the engine
    passed at evaluate time."""

    def _evaluate(event: dict[str, Any]) -> dict[str, Any]:
        bucket["event"] = dict(event)  # copy to capture snapshot
        return {"result": "ALLOW"}

    spec = FunctionPolicySpec(
        name="capture",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(path="test.not.used"),  # build-time stub
    )
    return FunctionPolicy(spec, _evaluate)


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


# ── V0 event shape ──


@pytest.mark.asyncio
async def test_event_carries_v0_shape(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """FunctionPolicy callable receives a V0-shaped event dict
    with type, target, data, and context keys."""
    bucket: dict[str, Any] = {}
    policy = _capturing_policy(bucket)
    engine = _build(
        conversation_store,
        [policy],
        initial_labels={"integrity": "1"},
    )
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="hello"))
    event = bucket["event"]
    assert event["type"] == "request"
    assert event["target"] is None  # REQUEST phase has no tool_name
    assert event["data"] == "hello"
    assert "actor" in event["context"]


@pytest.mark.asyncio
async def test_event_context_usage_carries_total_cost_usd(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """``event["context"]["usage"]`` carries ``total_cost_usd``.

    A cost-budget policy reads
    ``event["context"]["usage"]["total_cost_usd"]``; this pins that the
    engine seeds it and the function adapter forwards it, so the
    ``UsageContext.total_cost_usd`` schema field reflects what actually
    flows at runtime. If the adapter dropped the key (or the engine
    default omitted it), the policy would see no cost and never fire —
    the assertion below would KeyError / mismatch.
    """
    bucket: dict[str, Any] = {}
    policy = _capturing_policy(bucket)
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[policy],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        initial_usage={
            "input_tokens": 1000,
            "output_tokens": 200,
            "total_tokens": 1200,
            "total_cost_usd": 0.42,
        },
        conversation_store=conversation_store,
    )
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="hi"))
    usage = bucket["event"]["context"]["usage"]
    # 0.42 is the seeded session cost — proves the priced figure reaches
    # the policy, not just the token counts.
    assert usage["total_cost_usd"] == 0.42
    assert usage["input_tokens"] == 1000


@pytest.mark.asyncio
async def test_event_tool_call_carries_target(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """On TOOL_CALL phase, event.target is the tool_name."""
    bucket: dict[str, Any] = {}

    def _evaluate(event: dict[str, Any]) -> dict[str, Any]:
        bucket["event"] = dict(event)
        return {"result": "ALLOW"}

    spec = FunctionPolicySpec(
        name="capture",
        on=[PhaseSelector(phase=Phase.TOOL_CALL)],
        function=FunctionRef(path="test.not.used"),
    )
    policy = FunctionPolicy(spec, _evaluate)
    engine = _build(conversation_store, [policy])
    await engine.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": "web_search", "arguments": {"q": "test"}},
            tool_name="web_search",
        ),
    )
    event = bucket["event"]
    assert event["type"] == "tool_call"
    assert event["target"] == "web_search"
    assert event["data"] == {"name": "web_search", "arguments": {"q": "test"}}


@pytest.mark.asyncio
async def test_event_context_carries_labels_snapshot(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """``event["context"]["labels"]`` carries the engine's label cache.

    The advisor cost-plan guard reads ``cost_control.plan`` from this
    field; if the engine stopped injecting labels (or the function
    adapter dropped the key), the guard would silently see no plan and
    never enforce — the value assertion below would fail.
    """
    bucket: dict[str, Any] = {}
    policy = _capturing_policy(bucket)
    engine = _build(
        conversation_store,
        [policy],
        initial_labels={"cost_control.plan": '{"v": 1}', "integrity": "1"},
    )
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="hi"))
    # Exact-mapping assertion: both seeded labels arrive, nothing else.
    assert bucket["event"]["context"]["labels"] == {
        "cost_control.plan": '{"v": 1}',
        "integrity": "1",
    }
    # The exposed dict is a defensive copy — mutating it must not
    # corrupt the engine's hot cache (a shared reference would).
    bucket["event"]["context"]["labels"]["integrity"] = "0"
    assert engine.labels["integrity"] == "1"


# ── Engine labels still work correctly ──


@pytest.mark.asyncio
async def test_engine_labels_accumulate_across_evaluations(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Engine's hot cache reflects label writes from prior
    evaluations. A subsequent evaluation sees the effects of a
    prior policy's set_labels."""
    bucket_1: dict[str, Any] = {}
    bucket_2: dict[str, Any] = {}

    policy_1 = _capturing_policy(bucket_1)
    policy_2 = _capturing_policy(bucket_2)

    engine = _build(
        conversation_store,
        [policy_1],
        initial_labels={"integrity": "1"},
    )

    # First evaluation.
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    assert engine.labels == {"integrity": "1"}

    # Apply a write outside evaluate — bumps the hot cache.
    engine.apply_label_writes({"integrity": "0"})

    # Swap in policy_2 and re-evaluate.
    engine.policies = [policy_2]
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    # Engine's labels reflect the update.
    assert engine.labels == {"integrity": "0"}


@pytest.mark.asyncio
async def test_label_writer_then_reader_in_same_evaluation(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A fixed policy writes integrity=0; a later FunctionPolicy in
    the same evaluate() call sees the engine's initial label state
    (context is built once). After evaluate, engine reflects the
    accumulated writes."""
    bucket: dict[str, Any] = {}

    # Policy 1: FunctionPolicy writes integrity=0.
    writer = make_fixed_policy(
        name="writer",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        action=PolicyAction.ALLOW,
        set_labels={"integrity": "0"},
    )

    # Policy 2: FunctionPolicy captures event.
    reader = _capturing_policy(bucket)

    engine = _build(
        conversation_store,
        [writer, reader],
        initial_labels={"integrity": "1"},
    )
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))

    # After evaluate, the cache reflects the accumulated writes.
    assert engine.labels == {"integrity": "0"}
    # The V0 event was received.
    assert bucket["event"]["type"] == "request"
