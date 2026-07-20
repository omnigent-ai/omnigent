"""Basic-turn prerequisite probe."""

from __future__ import annotations

from tests.harness_bench.driver import infra_failure_reason
from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.transport import Driver
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict


class BasicTurnProbe(CapabilityProbe):
    name = "basic_turn"
    title = "Basic turn"
    priority = Priority.P0
    applies_to = Applicability.BOTH

    async def run(self, driver: Driver, profile: BenchProfile) -> ProbeResult:
        result = await driver.run_basic_turn(profile.marker)
        if result.timed_out:
            return ProbeResult(
                Verdict.SKIPPED,
                note="turn did not complete within timeout; harness not exercisable",
            )
        infra = infra_failure_reason(result)
        if infra is not None:
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
            return ProbeResult(
                Verdict.SUPPORTED,
                note="text returned (marker not echoed; model drift)",
                detail={"text": result.text[:200]},
            )
        return ProbeResult(Verdict.UNSUPPORTED, note="completed but produced no text")
