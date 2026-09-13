"""Fork replay probe — does a cloned session retain usable conversation history?"""

from __future__ import annotations

from tests.harness_bench.driver import infra_failure_reason
from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.transport import Driver
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict


class ForkReplayProbe(CapabilityProbe):
    name = "fork_replay"
    title = "Fork replay"
    priority = Priority.P1
    applies_to = Applicability.BOTH

    async def run(self, driver: Driver, profile: BenchProfile) -> ProbeResult:
        result = await driver.run_fork_turn(profile.marker)
        detail = {
            "created": result.created,
            "history_copied": result.history_copied,
            "recalled": result.recalled,
            "text": result.text,
            "timed_out": result.timed_out,
        }
        if result.created and result.history_copied and result.recalled:
            return ProbeResult(
                Verdict.SUPPORTED,
                note="fork copied history and the clone recalled the original marker",
                detail=detail,
            )
        if result.error:
            infra = infra_failure_reason(result)
            return ProbeResult(Verdict.SKIPPED, note=infra or str(result.error), detail=detail)
        if result.timed_out:
            return ProbeResult(Verdict.SKIPPED, note="fork replay turn timed out", detail=detail)
        if not result.created:
            return ProbeResult(Verdict.UNSUPPORTED, note="fork endpoint did not create a clone")
        if not result.history_copied:
            return ProbeResult(Verdict.UNSUPPORTED, note="fork did not copy the source history")
        return ProbeResult(
            Verdict.UNSUPPORTED,
            note="fork copied history but the clone did not recall the marker",
            detail=detail,
        )
