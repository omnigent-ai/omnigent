"""
Test multi-period policy validation.

Verifies that build_policy_engine raises a clear error when multiple
period cost policies are configured, preventing silent correctness bugs.
"""

from __future__ import annotations

import pytest

from omnigent.runtime.policies.builder import build_policy_engine
from omnigent.spec.types import (
    AgentSpec,
    FunctionPolicySpec,
    FunctionRef,
    GuardrailsSpec,
    Phase,
    PhaseSelector,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def test_multiple_period_policies_rejected(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Multiple period cost policies should raise ValueError.

    Polly AI Review identified this as a blocking issue: if multiple period
    policies are configured (e.g., weekly + monthly), only the first one's
    cost is seeded into context, so the second policy silently reads the
    wrong period's data. The fix is to validate and reject this configuration.
    """
    # Two period policies with different periods
    policy_specs = [
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

    spec = AgentSpec(
        spec_version=1,
        name="test-agent",
        guardrails=GuardrailsSpec(policies=policy_specs),
    )
    conv = conversation_store.create_conversation()

    # Should raise ValueError about multiple period policies
    with pytest.raises(
        ValueError,
        match=r"Multiple period cost policies are not yet supported.*Found 2 policies",
    ):
        build_policy_engine(
            spec=spec,
            conversation_id=conv.id,
            conversation_store=conversation_store,
        )


def test_single_period_policy_allowed(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Single period policy should work fine.
    """
    policy_specs = [
        FunctionPolicySpec(
            name="monthly_budget",
            on=[PhaseSelector(phase=Phase.REQUEST)],
            function=FunctionRef(
                path="omnigent.policies.builtins.cost.user_period_cost_budget",
                arguments={"period": "month", "max_cost_usd": 200.0},
            ),
        ),
    ]

    spec = AgentSpec(
        spec_version=1,
        name="test-agent",
        guardrails=GuardrailsSpec(policies=policy_specs),
    )
    conv = conversation_store.create_conversation()

    # Should not raise
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    assert engine is not None


def test_daily_plus_period_policy_allowed(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    Daily cost policy + one period policy should work (daily uses different context).
    """
    policy_specs = [
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

    spec = AgentSpec(
        spec_version=1,
        name="test-agent",
        guardrails=GuardrailsSpec(policies=policy_specs),
    )
    conv = conversation_store.create_conversation()

    # Should not raise (daily uses user_daily_cost context, monthly uses user_period_cost)
    engine = build_policy_engine(
        spec=spec,
        conversation_id=conv.id,
        conversation_store=conversation_store,
    )
    assert engine is not None
