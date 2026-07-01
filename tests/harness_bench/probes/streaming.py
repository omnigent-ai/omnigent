"""Streaming probe — token-level deltas vs a single complete blob.

Maps to the matrix's ``deltas`` / ``complete-only`` distinction: more than
one ``response.output_text.delta`` for a multi-token reply means the
harness streams incrementally; a single delta means it forwarded the full
response in one chunk (``PARTIAL`` — "complete-only").

A single measurement is noisy: a harness that streams can occasionally
coalesce a short reply into one delta, which would otherwise read as a
false complete-only and drift against a declared SUPPORTED. So when the
first attempt shows a single delta, the probe retries once and only
concludes complete-only if it reproduces — "streams sometimes" resolves to
SUPPORTED, "never streams" to PARTIAL.
"""

from __future__ import annotations

from tests.harness_bench.driver import SdkInprocDriver, TurnResult, infra_failure_reason
from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict

# Long enough that a genuinely streaming harness emits many deltas and a
# one-chunk flush is clearly complete-only rather than just a short reply.
_PROMPT = "Count from 1 to 30 in words, one number per line, and add a short note after each."


class StreamingProbe(CapabilityProbe):
    name = "streaming"
    title = "Streaming"
    priority = Priority.P0
    applies_to = Applicability.BOTH

    async def run(self, driver: SdkInprocDriver, profile: BenchProfile) -> ProbeResult:
        result = await self._measure(driver)
        skipped = self._skip_reason(result)
        if skipped is not None:
            return ProbeResult(Verdict.SKIPPED, note=skipped)

        n = result.text_delta_count
        if n > 1:
            return ProbeResult(
                Verdict.SUPPORTED,
                note=f"{n} text deltas (token-level)",
                detail={"text_delta_count": n},
            )
        if n == 0:
            return ProbeResult(
                Verdict.UNSUPPORTED, note="no text deltas emitted", detail={"text_delta_count": 0}
            )

        # Exactly one delta is ambiguous: a streaming harness may have
        # coalesced this reply. Retry once before concluding complete-only.
        retry = await self._measure(driver)
        skipped = self._skip_reason(retry)
        if skipped is not None:
            # Could not confirm on retry — report the first (ambiguous) reading
            # as PARTIAL rather than inventing certainty.
            return ProbeResult(
                Verdict.PARTIAL,
                note="single delta; retry inconclusive",
                detail={"text_delta_count": n, "retry_skipped": skipped},
            )
        m = retry.text_delta_count
        if m > 1:
            return ProbeResult(
                Verdict.SUPPORTED,
                note=f"streamed on retry ({m} deltas); first reply coalesced into one",
                detail={"text_delta_count": n, "retry_delta_count": m},
            )
        return ProbeResult(
            Verdict.PARTIAL,
            note="single delta on two attempts (complete-only)",
            detail={"text_delta_count": n, "retry_delta_count": m},
        )

    async def _measure(self, driver: SdkInprocDriver) -> TurnResult:
        return await driver.run_turn(_PROMPT)

    @staticmethod
    def _skip_reason(result: TurnResult) -> str | None:
        """Reason the run cannot be used to measure streaming, or ``None``."""
        if result.timed_out:
            return "could not measure streaming (timed out)"
        infra = infra_failure_reason(result)
        if infra is not None:
            return infra
        if result.failed:
            return f"could not measure streaming (failed: {result.error})"
        return None
