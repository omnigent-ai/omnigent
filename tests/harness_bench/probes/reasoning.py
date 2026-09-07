"""Reasoning-forwarding probe."""

from __future__ import annotations

from tests.harness_bench.driver import infra_failure_reason
from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.transport import Driver
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict


class ReasoningProbe(CapabilityProbe):
    name = "reasoning"
    title = "Reasoning"
    priority = Priority.P1
    applies_to = Applicability.BOTH

    async def run(self, driver: Driver, profile: BenchProfile) -> ProbeResult:
        result = await driver.run_reasoning_turn()
        detail = {
            "reasoning_delta_count": result.reasoning_delta_count,
            "reasoning_item_count": result.reasoning_item_count,
            "completed": result.completed,
        }
        infra = infra_failure_reason(result)
        if infra is not None:
            return ProbeResult(Verdict.SKIPPED, note=infra, detail=detail)
        if result.reasoning_delta_count > 0:
            return ProbeResult(
                Verdict.SUPPORTED,
                note=f"{result.reasoning_delta_count} reasoning deltas forwarded",
                detail=detail,
            )
        if result.reasoning_item_count > 0:
            return ProbeResult(
                Verdict.SUPPORTED,
                note=f"{result.reasoning_item_count} reasoning items persisted",
                detail=detail,
            )
        if result.timed_out:
            return ProbeResult(Verdict.SKIPPED, note="reasoning turn timed out", detail=detail)
        if result.failed:
            return ProbeResult(
                Verdict.SKIPPED,
                note=f"reasoning turn failed: {result.error}",
                detail=detail,
            )
        return ProbeResult(
            Verdict.SKIPPED,
            note="turn completed without observable reasoning; model emission is inconclusive",
            detail=detail,
        )
