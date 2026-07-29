"""Policy-ALLOW probe — does an explicit ALLOW policy let a tool call through?

Drives a real ``action=allow`` tool_call policy and checks the call proceeded
while that policy was attached. The native hook has no positive ALLOW event, so
this measures non-blocking under an explicit policy rather than proving the hook
evaluated it. Transports with no policy surface return unmeasured and SKIP.
"""

from __future__ import annotations

from tests.harness_bench.driver import infra_failure_reason
from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.transport import Driver
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict


class PolicyAllowProbe(CapabilityProbe):
    name = "policy_allow"
    title = "Policy ALLOW"
    priority = Priority.P1
    applies_to = Applicability.BOTH

    async def run(self, driver: Driver, profile: BenchProfile) -> ProbeResult:
        result = await driver.run_policy_turn(action="allow")
        detail = {
            "tool_call_allowed": result.tool_call_allowed,
            "tool_calls": [tc.get("name") for tc in result.tool_calls],
            "completed": result.completed,
        }

        if result.tool_call_allowed:
            return ProbeResult(
                Verdict.SUPPORTED,
                note="tool call proceeded under an explicit ALLOW policy",
                detail=detail,
            )

        infra = infra_failure_reason(result)
        if infra is not None:
            return ProbeResult(Verdict.SKIPPED, note=infra, detail=detail)
        if result.timed_out:
            return ProbeResult(Verdict.SKIPPED, note="allow-policy turn timed out", detail=detail)
        if not result.tool_calls and not result.completed:
            return ProbeResult(
                Verdict.SKIPPED,
                note=(
                    "ALLOW policy not observable on this transport "
                    "(or the model never attempted the tool)"
                ),
                detail=detail,
            )
        return ProbeResult(
            Verdict.SKIPPED,
            note="tool call did not visibly proceed under the ALLOW policy",
            detail=detail,
        )
