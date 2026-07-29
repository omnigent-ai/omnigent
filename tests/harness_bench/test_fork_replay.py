"""Verdict tests for the fork-history replay probe."""

from __future__ import annotations

from tests.harness_bench.driver import ForkResult
from tests.harness_bench.probes.fork_replay import ForkReplayProbe
from tests.harness_bench.profile import BenchProfile
from tests.harness_bench.verdict import Priority, Verdict

_PROFILE = BenchProfile(
    harness="claude-sdk",
    model="m",
    env_prefix="HARNESS_X_",
    marker="FORK_MARKER",
)


class _Driver:
    def __init__(self, result: ForkResult) -> None:
        self._result = result
        self.marker = ""

    async def run_fork_turn(self, marker: str) -> ForkResult:
        self.marker = marker
        return self._result


async def test_supported_when_clone_copies_and_replays_history() -> None:
    driver = _Driver(ForkResult(created=True, history_copied=True, recalled=True))

    result = await ForkReplayProbe().run(driver, _PROFILE)

    assert result.verdict is Verdict.SUPPORTED
    assert driver.marker == _PROFILE.marker


async def test_unsupported_when_history_was_not_copied() -> None:
    result = await ForkReplayProbe().run(
        _Driver(ForkResult(created=True, history_copied=False)),
        _PROFILE,
    )

    assert result.verdict is Verdict.UNSUPPORTED
    assert "did not copy" in result.note


async def test_skipped_when_transport_cannot_observe_fork() -> None:
    result = await ForkReplayProbe().run(
        _Driver(ForkResult(error="session fork is not observable on sdk-inproc")),
        _PROFILE,
    )

    assert result.verdict is Verdict.SKIPPED


def test_probe_is_report_only_p1() -> None:
    assert ForkReplayProbe().priority is Priority.P1
