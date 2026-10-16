"""The parked-message rendezvous judges a managed launch by its progress, not its length."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server import managed_hosts
from omnigent.server.managed_hosts import ManagedHostLaunch, ManagedLaunchTracker
from omnigent.server.routes._sessions.helpers import (
    _await_settled_managed_launch,
    _provision_managed_sandbox,
)

# Parked windows are 40% of the budget, so event-loop scheduling delay on a
# loaded runner cannot push a stage past it.
_BUDGET_S = 2.0
_WINDOW_S = 0.8
_SESSION = "conv_1"


@pytest.fixture(autouse=True)
def _short_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(managed_hosts, "MANAGED_LAUNCH_RENDEZVOUS_TIMEOUT_S", _BUDGET_S)
    monkeypatch.setattr(managed_hosts, "MANAGED_LAUNCH_PROGRESS_POLL_S", 0.01)


@pytest.fixture
async def waiters() -> AsyncIterator[list[asyncio.Task[None]]]:
    """Parked rendezvous tasks, cancelled after the test so a failed assertion
    leaves no pending task behind."""
    tasks: list[asyncio.Task[None]] = []
    yield tasks
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


def _begin(stage: str | None = None) -> ManagedLaunchTracker:
    tracker = ManagedLaunchTracker()
    tracker.begin(_SESSION)
    if stage is not None:
        tracker.advance(_SESSION, stage)
    return tracker


def _wait_on(
    tracker: ManagedLaunchTracker, waiters: list[asyncio.Task[None]]
) -> asyncio.Task[None]:
    launch = tracker.get(_SESSION)
    assert launch is not None
    task = asyncio.create_task(_await_settled_managed_launch(launch))
    waiters.append(task)
    return task


async def _still_parked(waiter: asyncio.Task[None], for_s: float) -> None:
    done, _ = await asyncio.wait({waiter}, timeout=for_s)
    if done:
        outcome = waiter.exception() or "resolved successfully"
        pytest.fail(f"the rendezvous ended while the launch was still in flight: {outcome!r}")


async def test_clone_longer_than_the_budget_keeps_the_message_parked(
    waiters: list[asyncio.Task[None]],
) -> None:
    """A launch may sit in ``cloning`` for longer than the whole stage budget."""
    tracker = _begin("cloning")
    waiter = _wait_on(tracker, waiters)
    await _still_parked(waiter, 3 * _BUDGET_S)
    tracker.advance(_SESSION, "starting")
    tracker.finish(_SESSION)
    await asyncio.wait_for(waiter, timeout=1.0)


async def test_long_clone_then_bounded_stage_counts_from_the_transition(
    waiters: list[asyncio.Task[None]],
) -> None:
    """After a clone outlives the ordinary budget, the next stage's budget starts at
    the transition, not at the clone's start."""
    tracker = _begin("cloning")
    waiter = _wait_on(tracker, waiters)
    await _still_parked(waiter, 2 * _BUDGET_S)
    tracker.advance(_SESSION, "starting")
    await _still_parked(waiter, _WINDOW_S)
    tracker.finish(_SESSION)
    await asyncio.wait_for(waiter, timeout=1.0)


async def test_clone_past_its_own_ceiling_still_gives_up(
    monkeypatch: pytest.MonkeyPatch, waiters: list[asyncio.Task[None]]
) -> None:
    """The clone budget is a ceiling, not an open-ended wait."""
    monkeypatch.setattr(managed_hosts, "MANAGED_LAUNCH_CLONE_RENDEZVOUS_TIMEOUT_S", 4 * _BUDGET_S)
    tracker = _begin("cloning")
    waiter = _wait_on(tracker, waiters)
    # Past the ordinary budget but well inside the clone ceiling.
    await _still_parked(waiter, _BUDGET_S + _WINDOW_S)
    with pytest.raises(OmnigentError) as excinfo:
        await asyncio.wait_for(waiter, timeout=6 * _BUDGET_S)
    assert excinfo.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert "still cloning" in str(excinfo.value)


async def test_repeated_clone_reports_restart_the_clone_budget(
    monkeypatch: pytest.MonkeyPatch, waiters: list[asyncio.Task[None]]
) -> None:
    """Each repository's clone report restarts the clone budget, so several
    repositories do not share one window."""
    monkeypatch.setattr(managed_hosts, "MANAGED_LAUNCH_CLONE_RENDEZVOUS_TIMEOUT_S", _BUDGET_S)
    tracker = _begin("cloning")
    waiter = _wait_on(tracker, waiters)
    window_s = _BUDGET_S - _WINDOW_S
    await _still_parked(waiter, window_s)
    tracker.advance(_SESSION, "cloning")
    # Past one clone budget in total; the second clone's own budget has room left.
    await _still_parked(waiter, window_s)
    tracker.finish(_SESSION)
    await asyncio.wait_for(waiter, timeout=1.0)


async def test_budget_restarts_at_every_stage(waiters: list[asyncio.Task[None]]) -> None:
    """Bounded stages that each fit the budget never add up to a timeout."""
    tracker = _begin()
    waiter = _wait_on(tracker, waiters)
    for stage in ("starting", "connecting"):
        await _still_parked(waiter, _WINDOW_S)
        tracker.advance(_SESSION, stage)
    # 1.2 budgets have passed in total; the current stage is well inside its own.
    await _still_parked(waiter, _WINDOW_S)
    tracker.finish(_SESSION)
    await asyncio.wait_for(waiter, timeout=1.0)


async def test_stalled_bounded_stage_still_gives_up() -> None:
    tracker = _begin("starting")
    launch = tracker.get(_SESSION)
    assert launch is not None
    with pytest.raises(OmnigentError) as excinfo:
        await asyncio.wait_for(_await_settled_managed_launch(launch), timeout=5 * _BUDGET_S)
    assert excinfo.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert "still starting" in str(excinfo.value)


async def test_failure_during_the_clone_surfaces_its_reason(
    waiters: list[asyncio.Task[None]],
) -> None:
    tracker = _begin("cloning")
    waiter = _wait_on(tracker, waiters)
    await _still_parked(waiter, 2 * _BUDGET_S)
    tracker.fail(_SESSION, "managed sandbox host startup failed: clone exited 128")
    with pytest.raises(OmnigentError) as excinfo:
        await asyncio.wait_for(waiter, timeout=1.0)
    assert excinfo.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert "clone exited 128" in str(excinfo.value)


def test_advance_replaces_the_progress_pair_and_ignores_unknown_or_settled_entries() -> None:
    """``advance`` publishes stage and timestamp as one value, and leaves unknown or
    settled entries untouched."""
    tracker = ManagedLaunchTracker()
    tracker.advance("conv_missing", "cloning")
    tracker.begin(_SESSION)
    launch = tracker.get(_SESSION)
    assert launch is not None
    seeded = launch.progress
    assert seeded[0] == "provisioning"
    tracker.advance(_SESSION, "cloning")
    assert launch.progress[0] == "cloning"
    assert launch.progress[1] >= seeded[1]
    tracker.fail(_SESSION, "boom")
    settled = launch.progress
    tracker.advance(_SESSION, "starting")
    assert launch.progress == settled


async def test_provision_relays_pipeline_stages_to_the_tracker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The launch pipeline's stage callback feeds the tracker as well as the SSE surface."""
    from omnigent.server.routes import sessions as facade

    published: list[str] = []
    monkeypatch.setattr(
        facade,
        "_publish_sandbox_status",
        lambda _session_id, stage, error=None: published.append(stage),
    )
    tracker = _begin()
    tracked: list[str] = []

    async def _fake_launch(*, on_stage, **_kwargs: object) -> ManagedHostLaunch:
        for stage in ("cloning", "starting"):
            on_stage(stage)
            launch = tracker.get(_SESSION)
            assert launch is not None
            tracked.append(launch.progress[0])
        return ManagedHostLaunch(host_id="host_1", workspace="/root/workspace/repo")

    monkeypatch.setattr(managed_hosts, "launch_managed_host", _fake_launch)
    result = await _provision_managed_sandbox(
        session_id=_SESSION,
        owner="alice@example.com",
        sandbox_config=SimpleNamespace(),
        repos=(),
        tracker=tracker,
        host_store=SimpleNamespace(),
        relaunch_host=None,
    )
    assert result is not None and result.host_id == "host_1"
    assert tracked == ["cloning", "starting"]
    assert published == ["cloning", "starting"]
