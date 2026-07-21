"""Tests for admission data exposed through the public function-policy seam."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.admission import AdmissionInfo
from omnigent.policies.function import FunctionPolicy
from omnigent.policies.types import EvaluationContext
from omnigent.spec.types import (
    FunctionPolicySpec,
    Phase,
    PhaseSelector,
    PolicyAction,
)


@pytest.mark.asyncio
async def test_function_policy_receives_admission_only_when_present() -> None:
    """The opt-in hook is visible without changing stock policy events."""
    observed: list[dict[str, Any]] = []

    async def capture(event: dict[str, Any]) -> dict[str, str]:
        observed.append(event)
        return {"result": "ALLOW"}

    policy = FunctionPolicy(
        FunctionPolicySpec(
            name="capture-admission",
            on=[PhaseSelector(phase=Phase.REQUEST)],
        ),
        capture,
    )

    stock_result = await policy.evaluate(
        EvaluationContext(phase=Phase.REQUEST, content="stock"),
        {},
    )
    admission = AdmissionInfo(
        admission_id="admission-1",
        input_seq=7,
        disposition="next_turn_buffer",
        lineage_id="lineage-1",
        active_response_id="response-1",
    )
    admitted_result = await policy.evaluate(
        EvaluationContext(
            phase=Phase.REQUEST,
            content="admitted",
            admission=admission,
        ),
        {},
    )

    assert stock_result.action == PolicyAction.ALLOW
    assert admitted_result.action == PolicyAction.ALLOW
    assert "admission" not in observed[0]["context"]
    assert observed[1]["context"]["admission"] == admission.to_dict()
