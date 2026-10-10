"""The parked-message rendezvous judges a managed launch by its progress, not its length."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server import managed_hosts
from omnigent.server.managed_hosts import ManagedHostLaunch, ManagedLaunchTracker
from omnigent.server.routes._sessions.helpers import (
    _await_settled_managed_launch,
    _provision_managed_sandbox,
)

_BUDGET_S = 0.5
_SESSION = "conv_1"


@pytest.fixture(autouse=True)
def _short_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(managed_hosts, "MANAGED_LAUNCH_RENDEZVOUS_TIMEOUT_S", _BUDGET_S)
    monkeypatch.setattr(managed_hosts, "MANAGED_LAUNCH_PROGRESS_POLL_S", 0.01, raising=False)


def _begin(stage: str | None = None) -> ManagedLaunchTracker:
    tracker = ManagedLaunchTracker()
    tracker.begin(_SESSION)
    if stage is not None:
        tracker.advance(_SESSION, stage)
    return tracker


def _wait_on(tracker: ManagedLaunchTracker) -> asyncio.Task[None]:
    launch = tracker.get(_SESSION)
    assert launch is not None
    return asyncio.create_task(_await_settled_managed_launch(launch))


async def _still_parked(waiter: asyncio.Task[None], for_s: float) -> None:
    done, _ = await asyncio.wait({waiter}, timeout=for_s)
    assert not done, "the rendezvous resolved while the launch was still in flight"


async def test_clone_longer_than_the_budget_keeps_the_message_parked() -> None:
    """A launch may sit in ``cloning`` for longer than the whole budget."""
    tracker = _begin("cloning")
    waiter = _wait_on(tracker)
    await _still_parked(waiter, 3 * _BUDGET_S)
    tracker.advance(_SESSION, "starting")
    tracker.finish(_SESSION)
    await asyncio.wait_for(waiter, timeout=1.0)


async def test_budget_restarts_at_every_stage() -> None:
    """Bounded stages that each fit the budget never add up to a timeout."""
    tracker = _begin()
    waiter = _wait_on(tracker)
    for stage in ("starting", "connecting"):
        await _still_parked(waiter, 0.4 * _BUDGET_S)
        tracker.advance(_SESSION, stage)
    # 1.2 budgets have passed in total; the current stage is well inside its own.
    await _still_parked(waiter, 0.4 * _BUDGET_S)
    tracker.finish(_SESSION)
    await asyncio.wait_for(waiter, timeout=1.0)


async def test_stalled_bounded_stage_still_gives_up() -> None:
    tracker = _begin("starting")
    launch = tracker.get(_SESSION)
    assert launch is not None
    with pytest.raises(OmnigentError) as excinfo:
        await asyncio.wait_for(_await_settled_managed_launch(launch), timeout=5 * _BUDGET_S)
    assert excinfo.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert "still provisioning" in str(excinfo.value)


async def test_failure_during_the_clone_surfaces_its_reason() -> None:
    tracker = _begin("cloning")
    waiter = _wait_on(tracker)
    await _still_parked(waiter, 2 * _BUDGET_S)
    tracker.fail(_SESSION, "managed sandbox host startup failed: clone exited 128")
    with pytest.raises(OmnigentError) as excinfo:
        await asyncio.wait_for(waiter, timeout=1.0)
    assert excinfo.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert "clone exited 128" in str(excinfo.value)


def test_advance_ignores_unknown_and_settled_entries() -> None:
    tracker = ManagedLaunchTracker()
    tracker.advance("conv_missing", "cloning")
    tracker.begin(_SESSION)
    tracker.fail(_SESSION, "boom")
    launch = tracker.get(_SESSION)
    assert launch is not None
    before = launch.progressed_at
    tracker.advance(_SESSION, "cloning")
    assert launch.stage == "provisioning"
    assert launch.progressed_at == before


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
            tracked.append(launch.stage)
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
