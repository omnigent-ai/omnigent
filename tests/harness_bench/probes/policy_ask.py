"""Policy-ASK probe — does an ASK policy raise an elicitation for approval?

Drives an ``action=ask`` tool_call policy and checks the call parked on an
elicitation (SSE ``response.elicitation_request`` — the same signal the web UI's
approval prompt uses); the driver resolves it so the turn settles. Full-server
observes it; transports with no elicitation surface return unmeasured and SKIP.
"""

from __future__ import annotations

from tests.harness_bench.driver import infra_failure_reason
from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.transport import Driver
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict


class PolicyAskProbe(CapabilityProbe):
    name = "policy_ask"
    title = "Policy ASK"
    priority = Priority.P1
    applies_to = Applicability.BOTH

    async def run(self, driver: Driver, profile: BenchProfile) -> ProbeResult:
        result = await driver.run_policy_turn(action="ask")
        detail = {
            "elicitation_requested": result.elicitation_requested,
            "tool_calls": [tc.get("name") for tc in result.tool_calls],
            "completed": result.completed,
        }

        if result.elicitation_requested:
            return ProbeResult(
                Verdict.SUPPORTED,
                note="ASK policy raised an elicitation (approval prompt) for the tool call",
                detail=detail,
            )

        infra = infra_failure_reason(result)
        if infra is not None:
            return ProbeResult(Verdict.SKIPPED, note=infra, detail=detail)
        if result.timed_out:
            return ProbeResult(Verdict.SKIPPED, note="ask-policy turn timed out", detail=detail)
        if not result.tool_calls and not result.completed:
            return ProbeResult(
                Verdict.SKIPPED,
                note=(
                    "ASK policy not observable on this transport "
                    "(or the model never attempted the tool)"
                ),
                detail=detail,
            )
        return ProbeResult(
            Verdict.SKIPPED,
            note="tool call did not raise a visible elicitation under the ASK policy",
            detail=detail,
        )
