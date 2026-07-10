"""Unit tests for the cost_tracking probe + the shared snapshot-cost reader.

Network-free: drives the probe with fake drivers returning canned TurnResults,
so the verdict logic (priced -> SUPPORTED, unpriced -> PARTIAL, no usage / infra
/ timeout -> SKIPPED) is asserted without a live gateway.
"""

from __future__ import annotations

from tests.harness_bench.driver import TurnResult, fill_snapshot_cost
from tests.harness_bench.probes.cost_tracking import CostTrackingProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Priority, Verdict

_PROFILE = BenchProfile(harness="fake", model="m", env_prefix="HARNESS_FAKE_", marker="MARK")


class _Driver:
    """Fake driver whose run_basic_turn returns a preset TurnResult."""

    transport = "full-server"

    def __init__(self, result: TurnResult) -> None:
        self._result = result

    async def run_basic_turn(self, marker: str) -> TurnResult:
        return self._result


async def _run(result: TurnResult):
    return await CostTrackingProbe().run(_Driver(result), _PROFILE)


def test_priority_is_p1() -> None:
    # P1 = reported, not merge-gating; also why it needs no declared verdict.
    assert CostTrackingProbe().priority is Priority.P1


async def test_priced_cost_is_supported() -> None:
    r = await _run(TurnResult(completed=True, text="ok", total_cost_usd=0.0123, total_tokens=1500))
    assert r.verdict is Verdict.SUPPORTED
    assert "0.012" in r.note


async def test_tokens_only_is_partial() -> None:
    # Unpriced model: usage visible, no USD cost -> PARTIAL (not a failure).
    r = await _run(TurnResult(completed=True, text="ok", total_cost_usd=None, total_tokens=900))
    assert r.verdict is Verdict.PARTIAL
    assert "900 tokens" in r.note


async def test_no_usage_is_skipped() -> None:
    # Completed but the transport surfaced nothing -> SKIP, never UNSUPPORTED.
    r = await _run(TurnResult(completed=True, text="ok"))
    assert r.verdict is Verdict.SKIPPED
    assert "no usage" in r.note


async def test_zero_cost_and_tokens_is_skipped() -> None:
    # A completed turn always spends tokens; a reported 0/0 means an empty
    # default from the usage plumbing, not a genuine zero -> SKIP, not a false
    # SUPPORTED/PARTIAL. Guards against the `is not None` false positive.
    r = await _run(TurnResult(completed=True, text="ok", total_cost_usd=0.0, total_tokens=0))
    assert r.verdict is Verdict.SKIPPED


async def test_zero_cost_but_positive_tokens_is_partial() -> None:
    # Cost defaulted to 0 (unpriced) but real tokens were counted -> PARTIAL,
    # not a false SUPPORTED on the 0 cost.
    r = await _run(TurnResult(completed=True, text="ok", total_cost_usd=0.0, total_tokens=900))
    assert r.verdict is Verdict.PARTIAL


async def test_infra_failure_is_skipped() -> None:
    r = await _run(TurnResult(failed=True, error={"message": "403 Forbidden"}))
    assert r.verdict is Verdict.SKIPPED


async def test_timeout_is_skipped() -> None:
    r = await _run(TurnResult(timed_out=True))
    assert r.verdict is Verdict.SKIPPED


def test_fill_snapshot_cost_reads_session_fields() -> None:
    r = TurnResult(completed=True)
    fill_snapshot_cost(r, {"total_cost_usd": 0.5, "last_total_tokens": 4200})
    assert r.total_cost_usd == 0.5
    assert r.total_tokens == 4200


def test_fill_snapshot_cost_tolerates_missing_and_null() -> None:
    r = TurnResult(completed=True)
    fill_snapshot_cost(r, {"total_cost_usd": None})  # unpriced snapshot
    assert r.total_cost_usd is None
    assert r.total_tokens is None
