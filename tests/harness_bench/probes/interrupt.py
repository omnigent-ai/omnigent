"""Interrupt probe — can a running turn be cancelled mid-stream?

Starts a long generation, posts an ``interrupt`` event the moment text
begins streaming, and checks the turn stops early rather than running to
its natural end. A harness that ignores the interrupt streams the whole
long reply; one that honors it terminates with far less output.
"""

from __future__ import annotations

from tests.harness_bench.driver import infra_failure_reason
from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.transport import Driver
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict


class InterruptProbe(CapabilityProbe):
    name = "interrupt"
    title = "Interrupt"
    priority = Priority.P0
    applies_to = Applicability.BOTH

    async def run(self, driver: Driver, profile: BenchProfile) -> ProbeResult:
        result = await driver.run_interrupt_turn()
        detail = {
            "chars": len(result.text),
            "completed": result.completed,
            "cancelled": result.cancelled,
            "failed": result.failed,
            "timed_out": result.timed_out,
        }
        # Explicit cancellation is transport-independent proof.
        if result.cancelled:
            return ProbeResult(
                Verdict.SUPPORTED,
                note=f"turn cancelled after interrupt ({len(result.text)} chars streamed)",
                detail=detail,
            )

        # Without deltas, the transport cannot prove the interrupt was exercised.
        if result.text_delta_count == 0:
            infra = infra_failure_reason(result)
            note = infra or "turn produced no text before terminating; interrupt not exercised"
            return ProbeResult(Verdict.SKIPPED, note=note, detail=detail)
        if result.timed_out:
            return ProbeResult(
                Verdict.UNSUPPORTED,
                note="turn kept running after interrupt (timed out)",
                detail=detail,
            )
        # A short terminal response indicates generation was cut off.
        if result.reached_terminal and len(result.text) < 800:
            return ProbeResult(
                Verdict.SUPPORTED,
                note=f"turn stopped early after interrupt ({len(result.text)} chars)",
                detail=detail,
            )
        if result.reached_terminal:
            return ProbeResult(
                Verdict.PARTIAL,
                note=(
                    f"terminated but full-length output ({len(result.text)} chars); "
                    "interrupt may have raced turn end"
                ),
                detail=detail,
            )
        return ProbeResult(Verdict.UNKNOWN, note="no terminal event and no timeout", detail=detail)
