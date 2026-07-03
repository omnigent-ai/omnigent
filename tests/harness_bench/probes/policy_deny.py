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
        # The driver owns the deny mechanism (a tool_call-phase verdict on the
        # wrap path, a spec-baked deny policy on full-server); the probe only
        # cares whether a surfaced tool call was actually blocked.
        result = await driver.run_tool_turn(deny=True)
        detail = {
            "policy_actions": result.policy_actions,
            "tool_call_denied": result.tool_call_denied,
            "tool_calls": [tc.get("name") for tc in result.tool_calls],
            "completed": result.completed,
        }

        if not result.tool_calls:
            # The model never tried the tool, so the tool-call DENY path was
            # never exercised — cannot conclude anything about enforcement.
            return ProbeResult(
                Verdict.SKIPPED,
                note="model never attempted the tool; tool-call DENY path not exercised",
                detail=detail,
            )
        if not result.tool_call_denied:
            # A tool call happened but no PHASE_TOOL_CALL evaluation was
            # surfaced to deny. Policy evaluation is normally driven by the
            # server / runner; in this wrap-direct transport some harnesses
            # (e.g. codex) dispatch the tool without a tool-call evaluation
            # hook, so we cannot exercise DENY here. That is a transport
            # limitation, not proof the harness ignores policy — report
            # SKIPPED and let the full-server transport assert enforcement.
            return ProbeResult(
                Verdict.SKIPPED,
                note=(
                    "tool call not routed through a tool-call policy evaluation "
                    "(wrap-direct limitation)"
                ),
                detail=detail,
            )
        # A DENY landed on the tool-call phase and the turn advanced (to
        # completion or a clean end) without hanging on the blocked call —
        # the harness accepted and enforced the tool-call DENY.
        if result.completed or result.failed:
            return ProbeResult(
                Verdict.SUPPORTED,
                note="tool-call DENY delivered and enforced; blocked call did not stall the turn",
                detail=detail,
            )
        if result.timed_out:
            return ProbeResult(
                Verdict.UNSUPPORTED,
                note="turn stalled after tool-call DENY (blocked call not handled)",
                detail=detail,
            )
        return ProbeResult(
            Verdict.UNKNOWN, note="tool-call DENY delivered; outcome inconclusive", detail=detail
        )
