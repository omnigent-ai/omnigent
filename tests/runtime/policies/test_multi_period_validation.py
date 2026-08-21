"""
Test multi-period policy validation.

Verifies that build_policy_engine raises a clear error when multiple
period cost policies are configured, preventing silent correctness bugs.
"""

from __future__ import annotations

import pytest

from omnigent.runtime.policies.builder import build_policy_engine
from omnigent.spec.types import FunctionPolicySpec, FunctionRef, Phase, PhaseSelector
from omnigent.stores.conversation_store.memory_store import MemoryConversationStore


def test_multiple_period_policies_rejected() -> None:
    """
    Multiple period cost policies should raise ValueError.

    Polly AI Review identified this as a blocking issue: if multiple period
    policies are configured (e.g., weekly + monthly), only the first one's
    cost is seeded into context, so the second policy silently reads the
    wrong period's data. The fix is to validate and reject this configuration.
    """
    store = MemoryConversationStore()

    # Two period policies with different periods
    specs = [
        FunctionPolicySpec(
            name="weekly_budget",
            on=[PhaseSelector(phase=Phase.REQUEST)],
            function=FunctionRef(
                path="omnigent.policies.builtins.cost.user_period_cost_budget",
                arguments={"period": "week", "max_cost_usd": 50.0},
            ),
        ),
        FunctionPolicySpec(
            name="monthly_budget",
            on=[PhaseSelector(phase=Phase.REQUEST)],
            function=FunctionRef(
                path="omnigent.policies.builtins.cost.user_period_cost_budget",
                arguments={"period": "month", "max_cost_usd": 200.0},
            ),
        ),
    ]

    # Should raise ValueError about multiple period policies
    with pytest.raises(
        ValueError,
        match=r"Multiple period cost policies are not yet supported.*Found 2 policies",
    ):
        build_policy_engine(
            conversation_id="test-conv",
            policy_specs=specs,
            conversation_store=store,
        )


def test_single_period_policy_allowed() -> None:
    """
    Single period policy should work fine.
    """
    store = MemoryConversationStore()

    specs = [
        FunctionPolicySpec(
            name="monthly_budget",
            on=[PhaseSelector(phase=Phase.REQUEST)],
            function=FunctionRef(
                path="omnigent.policies.builtins.cost.user_period_cost_budget",
                arguments={"period": "month", "max_cost_usd": 200.0},
            ),
        ),
    ]

    # Should not raise
    engine = build_policy_engine(
        conversation_id="test-conv",
        policy_specs=specs,
        conversation_store=store,
    )
    assert engine is not None


def test_daily_plus_period_policy_allowed() -> None:
    """
    Daily cost policy + one period policy should work (daily uses different context).
    """
    store = MemoryConversationStore()

    specs = [
        FunctionPolicySpec(
            name="daily_budget",
            on=[PhaseSelector(phase=Phase.REQUEST)],
            function=FunctionRef(
                path="omnigent.policies.builtins.cost.user_daily_cost_budget",
                arguments={"max_cost_usd": 10.0},
            ),
        ),
        FunctionPolicySpec(
            name="monthly_budget",
            on=[PhaseSelector(phase=Phase.REQUEST)],
            function=FunctionRef(
                path="omnigent.policies.builtins.cost.user_period_cost_budget",
                arguments={"period": "month", "max_cost_usd": 200.0},
            ),
        ),
    ]

    # Should not raise (daily uses user_daily_cost context, monthly uses user_period_cost)
    engine = build_policy_engine(
        conversation_id="test-conv",
        policy_specs=specs,
        conversation_store=store,
    )
    assert engine is not None
