"""Basic-turn probe — the prerequisite: does a plain turn produce text?

Every other behavioral probe presupposes this one passes. Its verdict
also tells a reader whether a red row is "this harness is broken" versus
"this one capability is missing".
"""

from __future__ import annotations

from tests.harness_bench.driver import SdkInprocDriver, infra_failure_reason
from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict


class BasicTurnProbe(CapabilityProbe):
    name = "basic_turn"
    title = "Basic turn"
    priority = Priority.P0
    applies_to = Applicability.BOTH

    async def run(self, driver: SdkInprocDriver, profile: BenchProfile) -> ProbeResult:
        result = await driver.run_turn(
            f"Reply with exactly the literal string {profile.marker} and nothing else."
        )
        if result.timed_out:
            # A timeout on the prerequisite turn is almost always a hung
            # environment, not a capability fact — do not call it UNSUPPORTED.
            return ProbeResult(
                Verdict.SKIPPED,
                note="turn did not complete within timeout; harness not exercisable",
            )
        infra = infra_failure_reason(result)
        if infra is not None:
            # Auth / gateway / connectivity failure — an environment problem,
            # reported as SKIPPED so it never shows up as capability drift.
            return ProbeResult(Verdict.SKIPPED, note=infra)
        if result.failed:
            return ProbeResult(Verdict.UNSUPPORTED, note=f"turn failed: {result.error}")
        if profile.marker in result.text:
            return ProbeResult(
                Verdict.SUPPORTED,
                note="marker echoed; round-trip works",
                detail={"chars": len(result.text)},
            )
        if result.text:
            # Text came back but not the marker — the wire path works, the
            # model just didn't follow instructions. Still a working turn.
            return ProbeResult(
                Verdict.SUPPORTED,
                note="text returned (marker not echoed; model drift)",
                detail={"text": result.text[:200]},
            )
        return ProbeResult(Verdict.UNSUPPORTED, note="completed but produced no text")
