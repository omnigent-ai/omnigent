"""
Sub-agent conversations must enforce the CHILD spec's own guardrails.

Every server-side engine-build site resolves a session's agent binding to
the ROOT bundle spec. For a ``kind="sub_agent"`` conversation the engine
therefore held only the parent's guardrail policies and silently dropped
the child's — a worker declaring a stricter
``max_tool_calls_per_session`` than its parent sailed past its own limit.

These tests pin the fixed contract at the builder layer:

- the child's guardrail policies join the engine for its conversation;
- the parent's policies still run (a looser child cannot weaken the
  parent's fence — effective limit is the stricter of the two);
- a child redeclaring a parent policy NAME does not replace the parent's
  instance (both run; DENY short-circuit keeps the stricter one decisive);
- top-level sessions and unresolvable child names are unaffected;
- the ``any_policies_apply`` fast path sees child-only guardrails;
- two limit-policy instances in one engine count each tool call ONCE.
"""

from __future__ import annotations

import pytest

from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.builder import any_policies_apply, build_policy_engine
from omnigent.spec.types import (
    AgentSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
    Phase,
    PolicyAction,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

_LIMIT_HANDLER = "omnigent.policies.builtins.safety.max_tool_calls_per_session"


def _limit_policy(name: str, limit: int) -> FunctionPolicySpec:
    """Build a ``max_tool_calls_per_session`` policy spec with *limit*."""
    return FunctionPolicySpec(
        name=name,
        on=None,
        function=FunctionRef(path=_LIMIT_HANDLER, arguments={"limit": limit}),
    )


def _bundle_spec(
    *,
    parent_limit: int | None,
    child_limit: int | None,
    child_policy_name: str = "child_tool_limit",
) -> AgentSpec:
    """A parent spec with a ``worker`` sub-agent, each optionally limited."""
    worker = AgentSpec(
        spec_version=1,
        name="worker",
        guardrails=(
            GuardrailsSpec(policies=[_limit_policy(child_policy_name, child_limit)])
            if child_limit is not None
            else None
        ),
    )
    return AgentSpec(
        spec_version=1,
        name="supervisor",
        guardrails=(
            GuardrailsSpec(policies=[_limit_policy("parent_tool_limit", parent_limit)])
            if parent_limit is not None
            else None
        ),
        sub_agents=[worker],
    )


def _tool_call(name: str = "calculate") -> EvaluationContext:
    """A TOOL_CALL evaluation context."""
    return EvaluationContext(
        phase=Phase.TOOL_CALL,
        content={"name": name, "arguments": {}},
        tool_name=name,
    )


def _make_child_conversation(
    store: SqlAlchemyConversationStore,
    sub_agent_name: str | None = "worker",
):
    """Create a root + sub-agent conversation pair; return the child row."""
    root = store.create_conversation()
    return store.create_conversation(
        kind="sub_agent",
        parent_conversation_id=root.id,
        sub_agent_name=sub_agent_name,
        title=f"{sub_agent_name}:limit-check" if sub_agent_name else None,
    )


# ── Engine composition ─────────────────────────────────────────


def test_child_guardrail_policies_join_the_subagent_engine(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """The worker's own policy must be present in its engine.

    Pre-fix the engine held only ``parent_tool_limit`` (plus the
    ``__ask_on_add_policy`` sentinel) — the child's declared guardrail was
    dropped at runtime.
    """
    child = _make_child_conversation(conversation_store)
    engine = build_policy_engine(
        spec=_bundle_spec(parent_limit=10, child_limit=3),
        conversation_id=child.id,
        conversation_store=conversation_store,
    )
    names = [p.spec.name for p in engine.policies]
    assert "child_tool_limit" in names, f"child guardrail dropped; got {names}"
    assert "parent_tool_limit" in names, f"parent guardrail dropped; got {names}"


def test_top_level_session_does_not_pick_up_child_policies(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """The PARENT's engine must not absorb the worker's guardrails."""
    conv = conversation_store.create_conversation()
    engine = build_policy_engine(
        spec=_bundle_spec(parent_limit=10, child_limit=3),
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    names = [p.spec.name for p in engine.policies]
    assert "child_tool_limit" not in names
    assert "parent_tool_limit" in names


def test_unresolvable_sub_agent_name_falls_back_to_parent_policies(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A display-label name (native harness children) resolves to no spec.

    The engine must build normally with the parent's policies only —
    never raise, never invent a child policy set.
    """
    child = _make_child_conversation(conversation_store, sub_agent_name="Explore codebase")
    engine = build_policy_engine(
        spec=_bundle_spec(parent_limit=10, child_limit=3),
        conversation_id=child.id,
        conversation_store=conversation_store,
    )
    names = [p.spec.name for p in engine.policies]
    assert "parent_tool_limit" in names
    assert "child_tool_limit" not in names


@pytest.mark.asyncio
async def test_same_name_looser_child_cannot_weaken_parent_fence(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A child redeclaring the parent's policy NAME with a looser limit
    must not replace the parent's stricter instance.

    Both instances run; the parent's DENY at its limit of 2 still
    short-circuits, so the child cannot self-exempt by name collision.
    """
    child = _make_child_conversation(conversation_store)
    # Parent limit 2, child redeclares the SAME name with limit 100.
    spec = _bundle_spec(parent_limit=2, child_limit=100, child_policy_name="tool_limit_shared")
    spec.guardrails.policies[0].name = "tool_limit_shared"  # type: ignore[union-attr, index]
    engine = build_policy_engine(
        spec=spec,
        conversation_id=child.id,
        conversation_store=conversation_store,
    )
    shared = [p for p in engine.policies if p.spec.name == "tool_limit_shared"]
    assert len(shared) == 2, "parent instance must survive a child name collision"
    for i in range(2):
        result = await engine.evaluate(_tool_call())
        assert result.action == PolicyAction.ALLOW, f"call {i + 1} should be allowed"
    denied = await engine.evaluate(_tool_call())
    assert denied.action == PolicyAction.DENY, (
        "the parent's stricter limit must still fence the child despite the "
        "same-name child policy declaring a looser limit"
    )
    assert "Exceeded 2 tool calls" in (denied.reason or "")


# ── Enforcement semantics ──────────────────────────────────────


@pytest.mark.asyncio
async def test_child_stricter_limit_denies_at_child_cap(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Parent 10 / child 3: the worker's 4th tool call is denied.

    The effective limit is the stricter of the two. Pre-fix all four calls
    were allowed because the child's policy never reached the engine.
    """
    child = _make_child_conversation(conversation_store)
    engine = build_policy_engine(
        spec=_bundle_spec(parent_limit=10, child_limit=3),
        conversation_id=child.id,
        conversation_store=conversation_store,
    )
    for i in range(3):
        result = await engine.evaluate(_tool_call())
        assert result.action == PolicyAction.ALLOW, f"call {i + 1} should be allowed"
    denied = await engine.evaluate(_tool_call())
    assert denied.action == PolicyAction.DENY
    assert "Exceeded 3 tool calls" in (denied.reason or "")


@pytest.mark.asyncio
async def test_parent_stricter_limit_still_fences_child(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Parent 2 / child 100: the inherited parent limit denies at 2.

    A child must not weaken the parent's fence — this behavior predates
    the fix and must survive it.
    """
    child = _make_child_conversation(conversation_store)
    engine = build_policy_engine(
        spec=_bundle_spec(parent_limit=2, child_limit=100),
        conversation_id=child.id,
        conversation_store=conversation_store,
    )
    for i in range(2):
        result = await engine.evaluate(_tool_call())
        assert result.action == PolicyAction.ALLOW, f"call {i + 1} should be allowed"
    denied = await engine.evaluate(_tool_call())
    assert denied.action == PolicyAction.DENY
    assert "Exceeded 2 tool calls" in (denied.reason or "")


@pytest.mark.asyncio
async def test_two_limit_instances_count_each_call_once(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """Both limit policies in one engine must not double-count a call.

    Both instances read the same pre-evaluation counter snapshot and write
    it back as ``snapshot + 1``; stacked INCREMENTs would advance the
    counter twice per call and halve every configured limit.
    """
    child = _make_child_conversation(conversation_store)
    engine = build_policy_engine(
        spec=_bundle_spec(parent_limit=10, child_limit=5),
        conversation_id=child.id,
        conversation_store=conversation_store,
    )
    result = await engine.evaluate(_tool_call())
    assert result.action == PolicyAction.ALLOW
    refreshed = conversation_store.get_conversation(child.id)
    assert refreshed is not None
    assert refreshed.session_state.get("_policy_tool_call_count") == 1, (
        "one tool call must advance the counter by exactly 1 even with two "
        "limit-policy instances in the engine"
    )


# ── Fast-path guard ────────────────────────────────────────────


def test_any_policies_apply_sees_child_only_guardrails(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A bundle whose ONLY guardrails live on the child must not fast-path.

    ``POST /policies/evaluate`` skips the engine build when
    ``any_policies_apply`` is False; with the root spec carrying no
    policies, a child-declared guardrail would never be enforced.
    """
    child = _make_child_conversation(conversation_store)
    spec = _bundle_spec(parent_limit=None, child_limit=3)
    assert any_policies_apply(
        spec=spec,
        conversation_id=child.id,
        default_policies=None,
        policy_store=None,
        phase=Phase.TOOL_CALL,
        tool_name="calculate",
        conversation=child,
    ), "child-only guardrails must defeat the no-policy fast path"
    # Without the conversation row (top-level callers), the root-spec-only
    # answer stands.
    top_level = conversation_store.create_conversation()
    assert not any_policies_apply(
        spec=spec,
        conversation_id=top_level.id,
        default_policies=None,
        policy_store=None,
        phase=Phase.TOOL_CALL,
        tool_name="calculate",
        conversation=top_level,
    )
