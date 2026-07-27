"""Tests for the reasoning-forwarding probe."""

from tests.harness_bench.driver import TurnResult
from tests.harness_bench.probes.reasoning import ReasoningProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Priority, Verdict

_PROFILE = BenchProfile(harness="fake", model="m", env_prefix="HARNESS_FAKE_", marker="MARK")


class _Driver:
    def __init__(self, result: TurnResult) -> None:
        self._result = result

    async def run_reasoning_turn(self) -> TurnResult:
        return self._result


async def _run(result: TurnResult):
    return await ReasoningProbe().run(_Driver(result), _PROFILE)


def test_priority_is_p1() -> None:
    assert ReasoningProbe().priority is Priority.P1


async def test_forwarded_reasoning_is_supported() -> None:
    result = await _run(TurnResult(completed=True, reasoning_delta_count=3))
    assert result.verdict is Verdict.SUPPORTED
    assert "3 reasoning deltas" in result.note


async def test_completed_without_reasoning_is_skipped() -> None:
    result = await _run(TurnResult(completed=True))
    assert result.verdict is Verdict.SKIPPED
    assert "model emission is inconclusive" in result.note


async def test_persisted_reasoning_is_supported() -> None:
    result = await _run(TurnResult(completed=True, reasoning_item_count=1))
    assert result.verdict is Verdict.SUPPORTED
    assert "reasoning items persisted" in result.note


async def test_timeout_and_infra_failure_are_skipped() -> None:
    timed_out = await _run(TurnResult(timed_out=True))
    auth = await _run(TurnResult(failed=True, error={"message": "403 Forbidden"}))
    assert timed_out.verdict is Verdict.SKIPPED
    assert auth.verdict is Verdict.SKIPPED
