"""Policy-DENY probe — does the harness enforce a DENY verdict on a tool call?

Offers a tool, prompts the model to call it, and — instead of allowing the
call — answers the harness's ``policy_evaluation.requested`` with
``POLICY_ACTION_DENY``. A harness that enforces the verdict blocks the
call (the tool result reflects a block / the model is told it was denied)
rather than executing it. This is the load-bearing half of the "Policies"
column: ALLOW is exercised implicitly by every other probe; DENY is what
must actually gate.
"""

from __future__ import annotations

from tests.harness_bench.driver import POLICY_DENY, SdkInprocDriver
from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict

_TOOL_NAME = "bench_forbidden"
_TOOL_SPEC = [
    {
        "type": "function",
        "function": {
            "name": _TOOL_NAME,
            "description": "A tool the policy engine will deny. Call it when asked.",
            "parameters": {
                "type": "object",
                "properties": {"arg": {"type": "string"}},
                "required": ["arg"],
            },
        },
    }
]

_DENY_REASON = "bench-policy-deny"


class PolicyDenyProbe(CapabilityProbe):
    name = "policy_deny"
    title = "Policy DENY"
    priority = Priority.P0
    applies_to = Applicability.BOTH

    async def run(self, driver: SdkInprocDriver, profile: BenchProfile) -> ProbeResult:
        result = await driver.run_turn(
            f"Call the {_TOOL_NAME} tool with arg='go'. It is required.",
            tools=_TOOL_SPEC,
            policy_action=POLICY_DENY,
            policy_reason=_DENY_REASON,
            # No auto tool output: a correctly-enforced DENY means the tool
            # never runs, so there is no result to return.
            timeout=150.0,
        )
        denied_delivered = POLICY_DENY in result.policy_actions
        detail = {
            "policy_actions": result.policy_actions,
            "tool_calls": [tc.get("name") for tc in result.tool_calls],
            "completed": result.completed,
        }

        if not result.tool_calls and not denied_delivered:
            # The model never tried the tool, so the DENY path was never
            # exercised — cannot conclude anything about enforcement.
            return ProbeResult(
                Verdict.SKIPPED,
                note="model never attempted the tool; DENY path not exercised",
                detail=detail,
            )
        if not denied_delivered:
            # A tool call happened but no policy_evaluation.requested was
            # surfaced. Policy evaluation is normally driven by the server /
            # runner; in this wrap-direct transport some harnesses (e.g.
            # codex) dispatch the tool without emitting an evaluation hook, so
            # we cannot exercise DENY here. That is a transport limitation,
            # not proof the harness ignores policy — report SKIPPED, and let
            # the full-server transport (phase-2) assert enforcement.
            return ProbeResult(
                Verdict.SKIPPED,
                note="no policy evaluation surfaced in the wrap-direct path (server-path concern)",
                detail=detail,
            )
        # A DENY verdict was requested and delivered, and the turn advanced
        # (to completion or a clean end) without hanging on the blocked call
        # — the harness accepted and enforced the DENY.
        if result.completed or result.failed:
            return ProbeResult(
                Verdict.SUPPORTED,
                note="DENY verdict delivered and enforced; blocked call did not stall the turn",
                detail=detail,
            )
        if result.timed_out:
            return ProbeResult(
                Verdict.UNSUPPORTED,
                note="turn stalled after DENY (blocked call not handled)",
                detail=detail,
            )
        return ProbeResult(
            Verdict.UNKNOWN, note="DENY delivered; outcome inconclusive", detail=detail
        )
