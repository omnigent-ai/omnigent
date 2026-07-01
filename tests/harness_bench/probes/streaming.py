"""Streaming probe — token-level deltas vs a single complete blob.

Maps to the matrix's ``deltas`` / ``complete-only`` distinction: more than
one ``response.output_text.delta`` for a multi-token reply means the
harness streams incrementally; exactly one means it forwards the full
response in a single chunk (``PARTIAL`` — "complete-only").
"""

from __future__ import annotations

from tests.harness_bench.driver import SdkInprocDriver
from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict


class StreamingProbe(CapabilityProbe):
    name = "streaming"
    title = "Streaming"
    priority = Priority.P0
    applies_to = Applicability.BOTH

    async def run(self, driver: SdkInprocDriver, profile: BenchProfile) -> ProbeResult:
        # A prompt long enough to span many tokens, so a streaming harness
        # emits clearly more than one delta.
        result = await driver.run_turn(
            "Count from 1 to 20 in words, one number per line.",
        )
        if result.timed_out or result.failed:
            reason = "timed out" if result.timed_out else f"failed: {result.error}"
            return ProbeResult(Verdict.SKIPPED, note=f"could not measure streaming ({reason})")

        n = result.text_delta_count
        detail = {"text_delta_count": n}
        if n > 1:
            return ProbeResult(
                Verdict.SUPPORTED, note=f"{n} text deltas (token-level)", detail=detail
            )
        if n == 1:
            return ProbeResult(Verdict.PARTIAL, note="single delta (complete-only)", detail=detail)
        return ProbeResult(Verdict.UNSUPPORTED, note="no text deltas emitted", detail=detail)
