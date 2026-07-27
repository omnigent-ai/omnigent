"""Verdict tests for the native Omnigent-MCP probe."""

from __future__ import annotations

from tests.harness_bench.driver import TurnResult
from tests.harness_bench.probes.omnigent_mcp import OmnigentMcpProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Applicability, Priority, Verdict

_PROFILE = BenchProfile(
    harness="claude-native",
    model="m",
    env_prefix="HARNESS_X_",
    marker="X",
    transport="native-tui",
)


class _Driver:
    def __init__(self, result: TurnResult) -> None:
        self._result = result

    async def run_mcp_tool_turn(self) -> TurnResult:
        return self._result


async def test_supported_for_prefixed_omnigent_tool_call() -> None:
    result = await OmnigentMcpProbe().run(
        _Driver(TurnResult(tool_calls=[{"name": "mcp__omnigent__sys_session_list"}])),
        _PROFILE,
    )

    assert result.verdict is Verdict.SUPPORTED


async def test_supported_for_bare_omnigent_tool_call() -> None:
    result = await OmnigentMcpProbe().run(
        _Driver(TurnResult(tool_calls=[{"name": "sys_session_list"}])),
        _PROFILE,
    )

    assert result.verdict is Verdict.SUPPORTED


async def test_skipped_for_unrelated_suffix_match() -> None:
    result = await OmnigentMcpProbe().run(
        _Driver(TurnResult(tool_calls=[{"name": "other_sys_session_list"}])),
        _PROFILE,
    )

    assert result.verdict is Verdict.SKIPPED


async def test_skipped_when_native_has_no_mcp_bridge() -> None:
    result = await OmnigentMcpProbe().run(
        _Driver(TurnResult(error="'pi-native' has no Omnigent MCP bridge")),
        _PROFILE,
    )

    assert result.verdict is Verdict.SKIPPED
    assert "no Omnigent MCP bridge" in result.note


async def test_skipped_when_model_calls_another_tool() -> None:
    result = await OmnigentMcpProbe().run(
        _Driver(TurnResult(completed=True, tool_calls=[{"name": "Bash"}])),
        _PROFILE,
    )

    assert result.verdict is Verdict.SKIPPED
    assert "did not call sys_session_list" in result.note


def test_probe_is_native_p1() -> None:
    probe = OmnigentMcpProbe()

    assert probe.priority is Priority.P1
    assert probe.applies_to is Applicability.NATIVE
