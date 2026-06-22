"""
Tests for PolicyEngine session_state — reading and writing per-turn
mutable state from function policy callables.

Verifies:
- Engine exposes current ``session_state`` via ``event["session_state"]``.
- ``PolicyResult.state_updates`` are shallow-merged into the hot cache.
- State accumulates within a single engine instance (visible on the next evaluation).
- Multiple policies in one pass accumulate updates in YAML order
  (last-write-wins per key).
- State updates are applied on DENY (not discarded on short-circuit).
- State updates are withheld on ASK (not applied until caller approves).
- Engine loaded with ``initial_session_state`` seeds the hot cache.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.llms.context_window import ModelPricing
from omnigent.policies.function import FunctionPolicy
from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import (
    FunctionPolicySpec,
    FunctionRef,
    Phase,
    PhaseSelector,
    PolicyAction,
    StateUpdateAction,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def _state_writing_policy(
    name: str,
    state_updates: dict[str, Any],
    *,
    action: str = "ALLOW",
) -> FunctionPolicy:
    """
    Build a :class:`FunctionPolicy` that returns fixed *state_updates*.

    :param name: Policy name, e.g. ``"counter_policy"``.
    :param state_updates: Key/value pairs to return as
        :attr:`PolicyResult.state_updates`.
    :param action: Decision string passed to the result, e.g. ``"ALLOW"``.
    :returns: A :class:`FunctionPolicy` ready for engine use.
    """

    def _evaluate(_event: dict[str, Any]) -> dict[str, Any]:
        return {"result": action, "state_updates": state_updates}

    spec = FunctionPolicySpec(
        name=name,
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(path="test.not.used"),
    )
    return FunctionPolicy(spec, _evaluate)


def _state_capturing_policy(bucket: dict[str, Any]) -> FunctionPolicy:
    """
    Build a :class:`FunctionPolicy` that records ``event["session_state"]``
    into *bucket* for assertion.

    :param bucket: Dict to write the captured state snapshot into under
        key ``"session_state"``.
    :returns: A capturing :class:`FunctionPolicy`.
    """

    def _evaluate(event: dict[str, Any]) -> dict[str, Any]:
        bucket["session_state"] = dict(event["session_state"])
        return {"result": "ALLOW"}

    spec = FunctionPolicySpec(
        name="capture",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(path="test.not.used"),
    )
    return FunctionPolicy(spec, _evaluate)


def _model_capturing_policy(bucket: dict[str, Any]) -> FunctionPolicy:
    """
    Build a :class:`FunctionPolicy` that records ``event["context"]["model"]``
    into *bucket* for assertion.

    :param bucket: Dict to write the captured model into under key
        ``"model"`` (present even when the value is ``None``).
    :returns: A capturing :class:`FunctionPolicy`.
    """

    def _evaluate(event: dict[str, Any]) -> dict[str, Any]:
        bucket["model"] = event["context"]["model"]
        return {"result": "ALLOW"}

    spec = FunctionPolicySpec(
        name="capture_model",
        on=[PhaseSelector(phase=Phase.TOOL_CALL)],
        function=FunctionRef(path="test.not.used"),
    )
    return FunctionPolicy(spec, _evaluate)


def _build_engine(
    store: SqlAlchemyConversationStore,
    policies: list[FunctionPolicy],
    *,
    initial_session_state: dict[str, Any] | None = None,
    initial_model: str | None = None,
) -> PolicyEngine:
    """
    Build a :class:`PolicyEngine` with a fresh conversation.

    :param store: Backing store used to create the conversation and
        handle label writes.
    :param policies: Ordered list of policies to run.
    :param initial_session_state: Seed state for the engine's hot cache,
        e.g. ``{"call_count": 3}``. ``None`` means start empty.
    :param initial_model: Seed model for the engine's model context,
        e.g. ``"opus"``. ``None`` means the model is undeterminable.
    :returns: A ready :class:`PolicyEngine`.
    """
    conv = store.create_conversation()
    return PolicyEngine(
        policies=policies,
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        initial_session_state=initial_session_state or {},
        initial_model=initial_model,
        conversation_store=store,
    )


# ── Reading session_state from the event ──────────────────────────────────────


@pytest.mark.asyncio
async def test_event_carries_session_state_key(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Function policy callables receive ``event["session_state"]`` as a
    dict. Defaults to an empty dict when no state has been written yet.

    What breaks if this fails: policies that read ``event["session_state"]``
    get a KeyError or ``None`` instead of the expected dict, breaking every
    stateful policy callable.
    """
    bucket: dict[str, Any] = {}
    engine = _build_engine(conversation_store, [_state_capturing_policy(bucket)])
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="hello"))
    assert bucket["session_state"] == {}


@pytest.mark.asyncio
async def test_event_session_state_reflects_initial_seed(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    When the engine is seeded with ``initial_session_state``, that state
    is visible in ``event["session_state"]`` on the first evaluation.

    What breaks if this fails: policies resuming a conversation lose the
    state accumulated in prior turns — every turn starts from scratch.
    """
    bucket: dict[str, Any] = {}
    engine = _build_engine(
        conversation_store,
        [_state_capturing_policy(bucket)],
        initial_session_state={"call_count": 3, "last_tool": "read_file"},
    )
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="hi"))
    assert bucket["session_state"] == {"call_count": 3, "last_tool": "read_file"}


# ── Injecting the active model ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_event_context_carries_injected_model(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    The engine injects ``initial_model`` into ``event["context"]["model"]``.

    What breaks if this fails: model-aware policies (e.g. the cost gate's
    force-downgrade branch) can never read the active model, so they
    cannot tell an expensive model from a cheap one and the downgrade
    gate is dead.
    """
    bucket: dict[str, Any] = {}
    engine = _build_engine(
        conversation_store,
        [_model_capturing_policy(bucket)],
        initial_model="databricks-claude-opus-4-8",
    )
    await engine.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": "sys_os_shell", "arguments": {}},
            tool_name="sys_os_shell",
        )
    )
    assert bucket["model"] == "databricks-claude-opus-4-8"


@pytest.mark.asyncio
async def test_event_context_model_is_none_when_unseeded(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    With no ``initial_model``, ``event["context"]["model"]`` is ``None``.

    What breaks if this fails: an undeterminable model would surface as a
    truthy MagicMock / missing key instead of ``None``, breaking the
    cost gate's "fail closed on unknown model" branch.
    """
    bucket: dict[str, Any] = {}
    engine = _build_engine(conversation_store, [_model_capturing_policy(bucket)])
    await engine.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": "sys_os_shell", "arguments": {}},
            tool_name="sys_os_shell",
        )
    )
    assert bucket["model"] is None


@pytest.mark.asyncio
async def test_caller_supplied_model_wins_over_engine_resolved(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A model already on the context is preferred over ``initial_model``.

    This is the race-free path for codex: the native hook reads the live
    model from ``config.toml`` at gate time and stamps it on the context.
    It must win over the engine's build-time resolution (which can lag
    behind a ``/model`` switch). What breaks if this fails: a user who
    downgrades via ``/model`` stays blocked because the gate keeps seeing
    the stale expensive model the engine resolved at build time.
    """
    bucket: dict[str, Any] = {}
    engine = _build_engine(
        conversation_store,
        [_model_capturing_policy(bucket)],
        initial_model="gpt-5.5",  # stale/expensive value resolved at build time
    )
    await engine.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": "sys_os_shell", "arguments": {}},
            tool_name="sys_os_shell",
            model="gpt-5.4",  # live value the hook read from config.toml
        )
    )
    # The hook-supplied gpt-5.4 reaches the policy, NOT the engine's gpt-5.5.
    assert bucket["model"] == "gpt-5.4"


# ── Writing state_updates ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_state_updates_merge_into_hot_cache(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A policy returning ``state_updates`` causes the engine's hot cache
    to reflect the merged state after evaluation.

    What breaks if this fails: ``engine.session_state`` never changes
    regardless of what policies return — state accumulation is silently
    broken.
    """
    policy = _state_writing_policy("counter", {"call_count": 1})
    engine = _build_engine(conversation_store, [policy])
    assert engine.session_state == {}
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    assert engine.session_state == {"call_count": 1}


@pytest.mark.asyncio
async def test_state_updates_shallow_merge_preserves_unmentioned_keys(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    state_updates are a shallow merge: keys not mentioned in the update
    are left untouched in the hot cache.

    What breaks if this fails: a policy updating ``call_count`` silently
    wipes out ``last_tool`` and any other previously-set keys.
    """
    policy = _state_writing_policy("updater", {"call_count": 5})
    engine = _build_engine(
        conversation_store,
        [policy],
        initial_session_state={"last_tool": "write_file", "call_count": 4},
    )
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    assert engine.session_state == {"last_tool": "write_file", "call_count": 5}


@pytest.mark.asyncio
async def test_multiple_policies_accumulate_state_updates_last_write_wins(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    When multiple policies in one evaluation pass return state_updates
    for the same key, the last policy in YAML order wins.

    What breaks if this fails: the first policy always wins, or updates
    are silently dropped, violating the documented merge semantics.
    """
    policy_a = _state_writing_policy("policy_a", {"x": "from_a", "y": "from_a"})
    policy_b = _state_writing_policy("policy_b", {"x": "from_b"})
    engine = _build_engine(conversation_store, [policy_a, policy_b])
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    # policy_b wins on "x"; "y" from policy_a is untouched.
    assert engine.session_state == {"x": "from_b", "y": "from_a"}


@pytest.mark.asyncio
async def test_state_updates_applied_on_deny(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    state_updates from a DENYing policy are still applied — consistent
    with how set_labels are applied on DENY.

    What breaks if this fails: a policy that both denies AND records
    audit state loses its state write, breaking audit trails on
    blocked requests.
    """
    policy = _state_writing_policy("deny_and_log", {"blocked": True}, action="DENY")
    engine = _build_engine(conversation_store, [policy])
    result = await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    assert result.action == PolicyAction.DENY
    # State was still applied despite the DENY.
    assert engine.session_state == {"blocked": True}


@pytest.mark.asyncio
async def test_state_updates_withheld_on_ask(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    state_updates from an ASKing policy are NOT applied to the hot cache —
    they are carried in the result for the caller to apply on approval only.

    What breaks if this fails: a policy that ASKs and is later denied would
    still apply its state writes, violating the §7.2 "no side effects from
    a denied ASK" invariant. The engine would accumulate state from every
    ASK, whether approved or denied.
    """
    policy = _state_writing_policy("ask_policy", {"flagged": True}, action="ASK")
    engine = _build_engine(conversation_store, [policy])
    result = await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="x"))
    assert result.action == PolicyAction.ASK
    # State NOT applied to the hot cache — withheld pending approval.
    assert engine.session_state == {}
    # State is carried in the result for callers to apply on approval.
    assert result.state_updates is not None
    assert len(result.state_updates) == 1
    assert result.state_updates[0].key == "flagged"
    assert result.state_updates[0].action == StateUpdateAction.SET
    assert result.state_updates[0].value is True


@pytest.mark.asyncio
async def test_second_evaluation_sees_prior_state_updates(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A callable that writes state on one evaluation sees that state in
    ``event["session_state"]`` on the next evaluation of the same engine.

    What breaks if this fails: within-turn state accumulation is broken —
    each policy sees an empty dict regardless of what earlier policies wrote.
    """
    write_bucket: dict[str, Any] = {}

    def _write_then_capture(event: dict[str, Any]) -> dict[str, Any]:
        write_bucket["seen"] = dict(event["session_state"])
        return {"result": "ALLOW", "state_updates": {"step": "done"}}

    spec = FunctionPolicySpec(
        name="write_then_capture",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(path="test.not.used"),
    )
    policy = FunctionPolicy(spec, _write_then_capture)
    engine = _build_engine(conversation_store, [policy], initial_session_state={"step": "start"})

    # First evaluation — sees initial seed, writes "done".
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="turn1"))
    assert write_bucket["seen"] == {"step": "start"}
    assert engine.session_state == {"step": "done"}

    # Second evaluation — sees the write from the first turn.
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="turn2"))
    assert write_bucket["seen"] == {"step": "done"}


# ── Usage tracking ────────────────────────────────────────────────────────────


def _usage_capturing_policy(bucket: dict[str, Any]) -> FunctionPolicy:
    """
    Build a :class:`FunctionPolicy` that records ``event["context"]["usage"]``
    into *bucket* for assertion.

    :param bucket: Dict to write the captured usage snapshot into under
        key ``"usage"``.
    :returns: A capturing :class:`FunctionPolicy`.
    """

    def _evaluate(event: dict[str, Any]) -> dict[str, Any]:
        bucket["usage"] = dict(event["context"]["usage"])
        return {"result": "ALLOW"}

    spec = FunctionPolicySpec(
        name="usage_capture",
        on=[PhaseSelector(phase=Phase.REQUEST)],
        function=FunctionRef(path="test.not.used"),
    )
    return FunctionPolicy(spec, _evaluate)


@pytest.mark.asyncio
async def test_engine_starts_with_zero_usage(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Engine starts with all-zero usage counters when no initial_usage is
    provided.

    What breaks if this fails: the usage property would be missing keys or
    start with non-zero values, causing budget-enforcement policies to
    miscount from the first turn.
    """
    engine = _build_engine(conversation_store, [])
    assert engine.usage == {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "total_cost_usd": 0.0,
    }


@pytest.mark.asyncio
async def test_record_usage_accumulates(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    After ``record_usage()`` calls, the engine's usage property reflects the
    cumulative values. Without pricing, total_cost_usd stays at 0.

    What breaks if this fails: usage counters would reset or not increment,
    making cumulative tracking useless for budget policies.
    """
    engine = _build_engine(conversation_store, [])
    engine.record_usage(input_tokens=100, output_tokens=50, total_tokens=150)
    assert engine.usage["input_tokens"] == 100
    assert engine.usage["output_tokens"] == 50
    assert engine.usage["total_tokens"] == 150
    assert engine.usage["cache_read_input_tokens"] == 0
    assert engine.usage["cache_creation_input_tokens"] == 0
    assert engine.usage["total_cost_usd"] == 0.0
    engine.record_usage(input_tokens=200, output_tokens=100, total_tokens=300)
    assert engine.usage["input_tokens"] == 300
    assert engine.usage["output_tokens"] == 150
    assert engine.usage["total_tokens"] == 450
    # Third call with cache tokens to verify accumulation.
    engine.record_usage(
        input_tokens=50,
        output_tokens=25,
        total_tokens=75,
        cache_read_input_tokens=3000,
        cache_creation_input_tokens=1000,
    )
    assert engine.usage["input_tokens"] == 350
    assert engine.usage["output_tokens"] == 175
    assert engine.usage["total_tokens"] == 525
    assert engine.usage["cache_read_input_tokens"] == 3000
    assert engine.usage["cache_creation_input_tokens"] == 1000


@pytest.mark.asyncio
async def test_record_usage_with_pricing_computes_cost(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    When ``token_pricing`` (:class:`ModelPricing`) is provided,
    ``record_usage()`` computes ``total_cost_usd`` from the per-token
    rates using :func:`compute_llm_cost`.

    What breaks if this fails: budget-enforcement policies that read
    ``event["context"]["usage"]["total_cost_usd"]`` would always see 0,
    making cost-based gating impossible.
    """
    conv = conversation_store.create_conversation()
    # $3/M input, $15/M output (Claude Sonnet pricing), no cache rates.
    pricing = ModelPricing(
        input_per_token=3.0 / 1_000_000,
        output_per_token=15.0 / 1_000_000,
    )
    engine = PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        token_pricing=pricing,
        conversation_store=conversation_store,
    )
    # 1000 input tokens @ $3/M = $0.003, 500 output tokens @ $15/M = $0.0075
    engine.record_usage(input_tokens=1000, output_tokens=500, total_tokens=1500)
    expected_cost = 1000 * 3.0 / 1_000_000 + 500 * 15.0 / 1_000_000
    assert engine.usage["total_cost_usd"] == pytest.approx(expected_cost)
    assert engine.usage["total_cost_usd"] == pytest.approx(0.0105)


@pytest.mark.asyncio
async def test_record_usage_with_cache_tokens_computes_cost(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    When ``ModelPricing`` includes cache-read and cache-write rates,
    ``record_usage()`` prices cache tokens at their own rates instead
    of the plain input rate.

    What breaks if this fails: cache-read tokens (cheap) and cache-write
    tokens (expensive) would be priced at the plain input rate, over- or
    under-counting cost for Anthropic-style providers.
    """
    conv = conversation_store.create_conversation()
    # Anthropic-style pricing: $3/M input, $15/M output,
    # $0.30/M cache-read (0.1x), $3.75/M cache-write (1.25x).
    pricing = ModelPricing(
        input_per_token=3.0 / 1_000_000,
        output_per_token=15.0 / 1_000_000,
        cache_read_per_token=0.30 / 1_000_000,
        cache_write_per_token=3.75 / 1_000_000,
    )
    engine = PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        token_pricing=pricing,
        conversation_store=conversation_store,
    )
    engine.record_usage(
        input_tokens=1000,
        output_tokens=500,
        total_tokens=1500,
        cache_read_input_tokens=5000,
        cache_creation_input_tokens=2000,
    )
    # 1000 * 3e-6 = 0.003  (non-cached input)
    # 500 * 15e-6 = 0.0075  (output)
    # 5000 * 0.3e-6 = 0.0015 (cache read)
    # 2000 * 3.75e-6 = 0.0075 (cache write)
    # total = 0.0195
    expected = (
        1000 * 3.0 / 1_000_000
        + 500 * 15.0 / 1_000_000
        + 5000 * 0.30 / 1_000_000
        + 2000 * 3.75 / 1_000_000
    )
    assert engine.usage["total_cost_usd"] == pytest.approx(expected)
    assert engine.usage["total_cost_usd"] == pytest.approx(0.0195)
    assert engine.usage["cache_read_input_tokens"] == 5000
    assert engine.usage["cache_creation_input_tokens"] == 2000


@pytest.mark.asyncio
async def test_event_context_carries_usage(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    The ``event["context"]["usage"]`` dict carries the current cumulative
    token counts when a function policy is dispatched.

    What breaks if this fails: policy callables that read
    ``event["context"]["usage"]`` would get stale or empty usage data,
    breaking budget-enforcement or rate-limiting policies.
    """
    bucket: dict[str, Any] = {}
    policy = _usage_capturing_policy(bucket)
    engine = _build_engine(conversation_store, [policy])
    engine.record_usage(input_tokens=500, output_tokens=200, total_tokens=700)
    await engine.evaluate(EvaluationContext(phase=Phase.REQUEST, content="hi"))
    assert bucket["usage"]["input_tokens"] == 500
    assert bucket["usage"]["output_tokens"] == 200
    assert bucket["usage"]["total_tokens"] == 700


@pytest.mark.asyncio
async def test_record_usage_persists_to_store(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    ``record_usage()`` writes the cumulative totals to the conversation's
    ``session_usage`` column so they survive across engine lifetimes.

    What breaks if this fails: usage counters reset to zero on every new
    turn because the persisted state is never written.
    """
    conv = conversation_store.create_conversation()
    engine = PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv.id,
        initial_labels={},
        conversation_store=conversation_store,
    )
    engine.record_usage(
        input_tokens=100,
        output_tokens=50,
        total_tokens=150,
        cache_read_input_tokens=400,
        cache_creation_input_tokens=200,
    )
    reloaded = conversation_store.get_conversation(conv.id)
    assert reloaded is not None
    assert reloaded.session_usage["input_tokens"] == 100
    assert reloaded.session_usage["output_tokens"] == 50
    assert reloaded.session_usage["total_tokens"] == 150
    assert reloaded.session_usage["cache_read_input_tokens"] == 400
    assert reloaded.session_usage["cache_creation_input_tokens"] == 200
    assert reloaded.session_usage["total_cost_usd"] == 0.0
