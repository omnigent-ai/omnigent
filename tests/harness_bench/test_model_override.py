"""Unit tests for the model_override probe.

Network-free: drives the probe with a completed TurnResult so the verdict
depends only on whether the driver applied a model override — not on a
live vendor CLI.
"""

from __future__ import annotations

from tests.harness_bench.driver import SdkInprocDriver, TurnResult
from tests.harness_bench.full_server_driver import FullServerDriver
from tests.harness_bench.native_tui_driver import NativeTuiDriver
from tests.harness_bench.probes.model_override import ModelOverrideProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Verdict

_PI_NATIVE = BenchProfile(
    harness="pi-native",
    model="databricks-claude-sonnet-4-6",
    env_prefix="HARNESS_PI_NATIVE_",
    marker="PI_NATIVE_OK",
    transport="native-tui",
)


class _CompletedTurnDriver:
    """Completed turn; override application is the only variable."""

    def __init__(self, *, applied_model_override: bool | None) -> None:
        if applied_model_override is not None:
            self.applied_model_override = applied_model_override

    async def run_basic_turn(self, marker: str) -> TurnResult:
        return TurnResult(completed=True, text=marker)


async def test_native_tui_completed_turn_is_not_supported_without_applied_override() -> None:
    """Reproduce the false SUPPORTED: native-TUI never sends the model.

    A completed turn on NativeTuiDriver is not evidence of model override.
    On current main this returns SUPPORTED; after the fix it must not.
    """
    driver = NativeTuiDriver(_PI_NATIVE, databricks_profile=None)

    async def _completed(marker: str) -> TurnResult:
        return TurnResult(completed=True, text=marker)

    driver.run_basic_turn = _completed  # type: ignore[method-assign]
    result = await ModelOverrideProbe().run(driver, _PI_NATIVE)

    assert result.verdict is not Verdict.SUPPORTED
    assert result.verdict in {Verdict.SKIPPED, Verdict.UNKNOWN}
    assert "does not accept a model override" in result.note


async def test_completed_turn_without_override_flag_is_not_supported() -> None:
    """Conservative default: a driver that does not expose the flag never SUPPORTED."""
    result = await ModelOverrideProbe().run(
        _CompletedTurnDriver(applied_model_override=None), _PI_NATIVE
    )
    assert result.verdict is not Verdict.SUPPORTED
    assert result.verdict in {Verdict.SKIPPED, Verdict.UNKNOWN}


async def test_completed_turn_with_applied_override_is_supported() -> None:
    result = await ModelOverrideProbe().run(
        _CompletedTurnDriver(applied_model_override=True), _PI_NATIVE
    )
    assert result.verdict is Verdict.SUPPORTED
    assert "databricks-claude-sonnet-4-6" in result.note


def test_native_tui_driver_reports_override_unused() -> None:
    driver = NativeTuiDriver(_PI_NATIVE, databricks_profile=None)
    assert driver.applied_model_override is False


def test_sdk_and_full_server_drivers_report_override_applied() -> None:
    assert SdkInprocDriver.applied_model_override is True
    assert FullServerDriver.applied_model_override is True
