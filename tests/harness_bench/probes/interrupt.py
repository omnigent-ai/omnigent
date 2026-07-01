"""Interrupt probe — can a running turn be cancelled mid-stream?

Starts a long generation, posts an ``interrupt`` event the moment text
begins streaming, and checks the turn stops early rather than running to
its natural end. A harness that ignores the interrupt streams the whole
long reply; one that honors it terminates with far less output.
"""

from __future__ import annotations

from tests.harness_bench.driver import SdkInprocDriver
from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict

# A prompt whose natural completion is long, so an honored interrupt
# produces visibly less output than letting it run.
_LONG_PROMPT = (
    "Write a detailed 400-word essay about the history of computing. Use full paragraphs."
)


class InterruptProbe(CapabilityProbe):
    name = "interrupt"
    title = "Interrupt"
    priority = Priority.P0
    applies_to = Applicability.BOTH

    async def run(self, driver: SdkInprocDriver, profile: BenchProfile) -> ProbeResult:
        result = await driver.run_turn(
            _LONG_PROMPT,
            interrupt_on_first_delta=True,
            timeout=120.0,
        )
        detail = {
            "chars": len(result.text),
            "completed": result.completed,
            "failed": result.failed,
            "timed_out": result.timed_out,
        }
        # An honored interrupt makes the stream reach a terminal event
        # (completed or failed/cancelled) promptly — the driver's timeout
        # did NOT fire — while having emitted only a fraction of the ~400
        # word target (a full essay is well over 1200 chars).
        reached_terminal = result.completed or result.failed
        if result.timed_out:
            return ProbeResult(
                Verdict.UNSUPPORTED,
                note="turn kept running after interrupt (timed out)",
                detail=detail,
            )
        if reached_terminal and len(result.text) < 800:
            return ProbeResult(
                Verdict.SUPPORTED,
                note=f"turn stopped early after interrupt ({len(result.text)} chars)",
                detail=detail,
            )
        if reached_terminal:
            # Terminated, but with a full-length body: the interrupt likely
            # landed after generation had already finished. Inconclusive.
            return ProbeResult(
                Verdict.PARTIAL,
                note=(
                    f"terminated but full-length output ({len(result.text)} chars); "
                    "interrupt may have raced turn end"
                ),
                detail=detail,
            )
        return ProbeResult(Verdict.UNKNOWN, note="no terminal event and no timeout", detail=detail)
