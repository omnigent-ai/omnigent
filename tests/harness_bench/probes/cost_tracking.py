"""Cost-tracking probe — does a completed turn report usage / cost?

Cost tracking is the keystone for cost *policies*: a ``cost_budget`` guardrail
(``omnigent/policies/builtins/cost.py``) is a no-op without usage to measure, so
this probe answers "can the operator see what a turn spent?".

It reads the cumulative usage the server records on the session: ``total_cost_usd``
(priced spend) and ``last_total_tokens`` (token count), surfaced on the session
snapshot (``SessionResponse``) and the ``session.usage`` SSE event. The bench
observes it via ``TurnResult.total_cost_usd`` / ``total_tokens``, which the
server-backed drivers fill from the snapshot after a turn and the wrap driver
fills from the completed turn's ``usage`` when the wrap forwards it.

Verdicts:
- **SUPPORTED** — a USD cost was reported (full cost tracking).
- **PARTIAL** — tokens reported but no priced cost (an unpriced model: usage is
  visible, so a token/budget policy works, but a USD-cost policy cannot price
  it). This matches omnigent's own behavior — an unpriced model makes the cost
  policy fail to ASK/DENY rather than silently allow.
- **SKIPPED** — the transport surfaced no usage at all (e.g. the wrap path when
  the harness forwards none), with the reason; never a false UNSUPPORTED.
"""

from __future__ import annotations

from tests.harness_bench.driver import infra_failure_reason
from tests.harness_bench.probes.base import CapabilityProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.transport import Driver
from tests.harness_bench.verdict import Applicability, Priority, ProbeResult, Verdict


class CostTrackingProbe(CapabilityProbe):
    name = "cost_tracking"
    title = "Cost tracking"
    # P1: reported, not merge-gating — a harness that doesn't surface cost is
    # still usable; the operator just can't run a USD-cost policy against it.
    priority = Priority.P1
    applies_to = Applicability.BOTH

    async def run(self, driver: Driver, profile: BenchProfile) -> ProbeResult:
        result = await driver.run_basic_turn(profile.marker)
        detail = {
            "total_cost_usd": result.total_cost_usd,
            "total_tokens": result.total_tokens,
            "completed": result.completed,
        }

        # An env/auth failure or timeout means we never got a clean turn to
        # measure — SKIP (never UNSUPPORTED), same contract as the other probes.
        infra = infra_failure_reason(result)
        if infra is not None:
            return ProbeResult(Verdict.SKIPPED, note=infra, detail=detail)
        if not result.completed:
            if result.timed_out:
                return ProbeResult(Verdict.SKIPPED, note="turn timed out", detail=detail)
            return ProbeResult(
                Verdict.SKIPPED,
                note=f"turn did not complete: {result.error}",
                detail=detail,
            )

        # Require a POSITIVE value, not merely non-None: a completed turn always
        # spends tokens, so a reported 0 cost / 0 tokens means the usage plumbing
        # returned an empty default, not that tracking genuinely measured zero.
        # Treating 0 as "supported" would be a false pass.
        if result.total_cost_usd is not None and result.total_cost_usd > 0:
            return ProbeResult(
                Verdict.SUPPORTED,
                note=f"turn reported cost ${result.total_cost_usd:.6f}",
                detail=detail,
            )
        if result.total_tokens is not None and result.total_tokens > 0:
            return ProbeResult(
                Verdict.PARTIAL,
                note=(
                    f"usage reported ({result.total_tokens} tokens) but no priced cost "
                    "(unpriced model): token/budget policy works, USD-cost policy cannot price it"
                ),
                detail=detail,
            )
        # Completed but no positive usage surfaced (absent, or a zero default) —
        # not a capability gap we can assert, so SKIP with the reason.
        return ProbeResult(
            Verdict.SKIPPED,
            note="turn completed but the transport surfaced no usage/cost to observe",
            detail=detail,
        )
