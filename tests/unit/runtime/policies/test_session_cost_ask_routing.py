"""Per-session cost-budget ASK approval is shared across the spawn tree.

The session cost-budget policy records its approved soft checkpoint under the
reserved ``SESSION_COST_ASK_APPROVED_STATE_KEY``. The budget is per-SESSION (the
whole spawn tree), but a sub-agent runs as its own conversation, so:

- :class:`PolicyEngine.apply_state_updates` routes the approval WRITE to the
  ROOT conversation, and
- :func:`build_policy_engine` SEEDS a sub-agent's approval from the root.

Without that, approving on the parent wouldn't carry to a sub-agent and it would
re-ASK at the same threshold (the reported bug).
"""

from __future__ import annotations

import pytest

from omnigent.policies.schema import SESSION_COST_ASK_APPROVED_STATE_KEY
from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.builder import build_policy_engine
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.parser import parse
from omnigent.spec.types import (
    FunctionPolicySpec,
    FunctionRef,
    Phase,
    PolicyAction,
    StateUpdate,
    StateUpdateAction,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

# A real session cost-budget policy: soft ASK at $0.05, hard cap parked high so
# only the soft checkpoint trips.
_COST_POLICY = FunctionPolicySpec(
    name="session_cost_guard",
    on=None,  # the function self-selects the tool_call phase
    function=FunctionRef(
        path="omnigent.policies.builtins.cost.cost_budget",
        arguments={"max_cost_usd": 1000.0, "ask_thresholds_usd": [0.05]},
    ),
)


def _engine_on(
    conversation_store: SqlAlchemyConversationStore,
    conversation_id: str,
    root_conversation_id: str,
) -> PolicyEngine:
    """Minimal engine bound to *conversation_id* with an explicit tree root."""
    return PolicyEngine(
        policies=[],
        label_defs={},
        ask_timeout=30,
        conversation_id=conversation_id,
        root_conversation_id=root_conversation_id,
        initial_labels={},
        conversation_store=conversation_store,
    )


def _set_approved(value: float) -> StateUpdate:
    return StateUpdate(
        key=SESSION_COST_ASK_APPROVED_STATE_KEY,
        action=StateUpdateAction.SET,
        value=value,
    )


def test_session_cost_ask_approval_routes_to_root_for_subagent(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A sub-agent's cost approval persists to the ROOT, not its own state."""
    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id
    )
    engine = _engine_on(conversation_store, child.id, parent.id)

    engine.apply_state_updates([_set_approved(0.05)])

    # Lands on the ROOT so the parent + sibling sub-agents inherit it. If this
    # is None, the approval was written to the sub-agent's own state and the
    # parent would re-prompt (the bug).
    root_after = conversation_store.get_conversation(parent.id)
    assert root_after.session_state.get(SESSION_COST_ASK_APPROVED_STATE_KEY) == 0.05
    # And NOT on the sub-agent's own session_state.
    child_after = conversation_store.get_conversation(child.id)
    assert SESSION_COST_ASK_APPROVED_STATE_KEY not in child_after.session_state


def test_session_cost_ask_approval_writes_own_state_for_top_level(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """A top-level session (root == itself) writes the approval to its own state.

    Guards that the root-routing doesn't break the ordinary single-session path.
    """
    root = conversation_store.create_conversation()
    engine = _engine_on(conversation_store, root.id, root.id)

    engine.apply_state_updates([_set_approved(0.05)])

    after = conversation_store.get_conversation(root.id)
    assert after.session_state.get(SESSION_COST_ASK_APPROVED_STATE_KEY) == 0.05


async def _evaluate_bash(engine: PolicyEngine) -> PolicyAction:
    result = await engine.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            content={"name": "Bash", "arguments": {}},
            tool_name="Bash",
        )
    )
    return result.action


@pytest.mark.asyncio
async def test_subagent_inherits_parent_cost_approval_no_reask(
    conversation_store: SqlAlchemyConversationStore,
    tmp_path,
) -> None:
    """Approving the $0.05 checkpoint on the parent suppresses the sub-agent's
    re-ASK at the same threshold (the reported bug).

    The sub-agent is its own conversation with $0.06 of spend (over $0.05);
    build_policy_engine seeds its approved-checkpoint from the root, so the
    gate ALLOWs instead of asking again.
    """
    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id
    )
    # Parent already approved the $0.05 checkpoint (recorded on the root).
    conversation_store.set_session_state(parent.id, {SESSION_COST_ASK_APPROVED_STATE_KEY: 0.05})
    # Sub-agent's own priced spend is over the $0.05 checkpoint.
    conversation_store.set_session_usage(child.id, {"total_cost_usd": 0.06})
    (tmp_path / "config.yaml").write_text("spec_version: 1\nname: cost-agent\n")
    spec = parse(tmp_path)
    engine = build_policy_engine(
        spec=spec,
        conversation_id=child.id,
        conversation_store=conversation_store,
        default_policies=[_COST_POLICY],
    )

    assert await _evaluate_bash(engine) == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_subagent_approval_visible_to_same_engine_no_reask(
    conversation_store: SqlAlchemyConversationStore,
    tmp_path,
) -> None:
    """Approving mid-turn suppresses the sub-agent's *next* re-ASK in the same
    engine instance.

    The approval write routes to the ROOT store, but the live engine evaluates
    against its own in-memory session_state (seeded from the root at
    construction). If that hot copy isn't updated on approve, the very next
    tool call re-ASKs at the same threshold — so this guards
    ``_record_root_cost_ask_approved`` mirroring the op into ``_session_state``.
    """
    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id
    )
    # Sub-agent is over the $0.05 checkpoint with NO prior approval anywhere.
    conversation_store.set_session_usage(child.id, {"total_cost_usd": 0.06})
    (tmp_path / "config.yaml").write_text("spec_version: 1\nname: cost-agent\n")
    spec = parse(tmp_path)
    engine = build_policy_engine(
        spec=spec,
        conversation_id=child.id,
        conversation_store=conversation_store,
        default_policies=[_COST_POLICY],
    )

    # First call asks (over threshold, unapproved).
    assert await _evaluate_bash(engine) == PolicyAction.ASK
    # The approval the user grants in response to that ASK.
    engine.apply_state_updates([_set_approved(0.05)])
    # The same engine's next call must NOT re-ASK — the in-memory state now
    # carries the approval even though it was persisted to the root.
    assert await _evaluate_bash(engine) == PolicyAction.ALLOW


@pytest.mark.asyncio
async def test_subagent_without_parent_approval_still_asks(
    conversation_store: SqlAlchemyConversationStore,
    tmp_path,
) -> None:
    """Control: with NO parent approval, the sub-agent's over-threshold spend
    DOES ASK — so the inheritance test above can't pass vacuously.
    """
    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id
    )
    conversation_store.set_session_usage(child.id, {"total_cost_usd": 0.06})
    (tmp_path / "config.yaml").write_text("spec_version: 1\nname: cost-agent\n")
    spec = parse(tmp_path)
    engine = build_policy_engine(
        spec=spec,
        conversation_id=child.id,
        conversation_store=conversation_store,
        default_policies=[_COST_POLICY],
    )

    assert await _evaluate_bash(engine) == PolicyAction.ASK


@pytest.mark.asyncio
async def test_subagent_gate_sees_parent_spend_not_just_own(
    conversation_store: SqlAlchemyConversationStore,
    tmp_path,
) -> None:
    """A sub-agent that spent $0 itself still ASKs when the SESSION is over budget.

    The budget is session-wide: the parent already spent $0.06 (over the $0.05
    checkpoint) and the sub-agent has spent nothing of its own. The sub-agent's
    gate must seed from the whole-tree total and ASK — otherwise (the bug) it
    would gate on its own $0 subtree, ALLOW, and let the session keep spending
    past the budget while the orchestrator parent is parked.

    If the gating seed reverts to the per-node subtree, the sub-agent sees $0,
    this returns ALLOW, and the assertion fails — so the test is non-vacuous.
    """
    parent = conversation_store.create_conversation()
    child = conversation_store.create_conversation(
        kind="sub_agent", parent_conversation_id=parent.id
    )
    # All spend is on the PARENT; the sub-agent has none of its own.
    conversation_store.set_session_usage(parent.id, {"total_cost_usd": 0.06})
    (tmp_path / "config.yaml").write_text("spec_version: 1\nname: cost-agent\n")
    spec = parse(tmp_path)
    engine = build_policy_engine(
        spec=spec,
        conversation_id=child.id,
        conversation_store=conversation_store,
        default_policies=[_COST_POLICY],
    )

    assert await _evaluate_bash(engine) == PolicyAction.ASK
