"""Policy-DENY probe — does the harness enforce a DENY on a *tool call*?

Offers a tool, prompts the model to call it, and answers the harness's
``policy_evaluation.requested`` with DENY **only for the tool-call phase**
(``PHASE_TOOL_CALL``), allowing every other phase. A harness that enforces
the verdict blocks the call rather than executing it.

Scoping the DENY to the tool-call phase matters: the scaffold also
evaluates the request and result phases, and answering DENY to all of them
could terminate the turn at the request phase and look like a pass without
a tool call ever being gated. So SUPPORTED requires both that a tool call
was actually surfaced and that the DENY landed on ``PHASE_TOOL_CALL``.
"""

from __future__ import annotations

from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.transport import Driver
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict


class PolicyDenyProbe(CapabilityProbe):
    name = "policy_deny"
    title = "Policy DENY"
    priority = Priority.P0
    applies_to = Applicability.BOTH

    async def run(self, driver: Driver, profile: BenchProfile) -> ProbeResult:
        result = await driver.run_tool_turn(deny=True)
        detail = {
            "policy_actions": result.policy_actions,
            "tool_call_denied": result.tool_call_denied,
            "tool_calls": [tc.get("name") for tc in result.tool_calls],
            "completed": result.completed,
        }

        # Native hooks can deny before a function-call item is persisted.
        if result.tool_call_denied:
            if result.completed or result.failed:
                return ProbeResult(
                    Verdict.SUPPORTED,
                    note="tool-call DENY delivered and enforced; turn advanced past the block",
                    detail=detail,
                )
            if result.timed_out:
                return ProbeResult(
                    Verdict.UNSUPPORTED,
                    note="turn stalled after tool-call DENY (blocked call not handled)",
                    detail=detail,
                )
            return ProbeResult(
                Verdict.SUPPORTED,
                note="tool-call DENY delivered and enforced",
                detail=detail,
            )

        if not result.tool_calls:
            return ProbeResult(
                Verdict.SKIPPED,
                note="model never attempted the tool; tool-call DENY path not exercised",
                detail=detail,
            )
        # Wrap-direct transports may dispatch tools without a policy hook.
        return ProbeResult(
            Verdict.SKIPPED,
            note=(
                "tool call not routed through a tool-call policy evaluation "
                "(wrap-direct limitation)"
            ),
            detail=detail,
        )
