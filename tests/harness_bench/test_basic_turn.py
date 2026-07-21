"""Verdict tests for the Basic turn prerequisite."""

from __future__ import annotations

from tests.harness_bench.driver import TurnResult
from tests.harness_bench.probes.basic_turn import BasicTurnProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Verdict

_PROFILE = BenchProfile(
    harness="qwen-native",
    model="m",
    env_prefix="HARNESS_QWEN_NATIVE_",
    marker="QWEN_NATIVE_OK",
    transport="native-tui",
)


class _Driver:
    async def run_basic_turn(self, marker: str) -> TurnResult:
        return TurnResult(completed=True, text="[API Error: 403 Invalid Token]")


async def test_textual_auth_error_skips_prerequisite() -> None:
    result = await BasicTurnProbe().run(_Driver(), _PROFILE)

    assert result.verdict is Verdict.SKIPPED
    assert "403" in result.note
