"""Tests for the managed-path sandbox keepalive.

Covers the resolution chain (runner -> session -> host -> provider), the
per-runner rate limit, the two skip paths (provider can't extend, host has no
sandbox), and the scheduler: a bounded worker pool, one outstanding attempt per
runner, and the tunnel loop's remaining-due-time wake. Stubs stand in for the
stores/deployment: the module only reads a few attributes off each, so a real
store would add setup without adding cover.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.onboarding.sandboxes.base import (
    MANAGED_KEEPALIVE_INTERVAL_ENV_VAR,
    SandboxCapabilityError,
)
from omnigent.server import managed_host_keepalive
from omnigent.server.routes import runner_tunnel


class _Launcher:
    def __init__(self, raises: BaseException | None = None, returns: object = None) -> None:
        self.calls: list[str] = []
        self._raises = raises
        self._returns = returns

    def keep_alive(self, sandbox_id: str) -> object:
        self.calls.append(sandbox_id)
        if self._raises is not None:
            raise self._raises
        return self._returns


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    launcher: _Launcher,
    host: object | None,
    host_id: str | None = "host1",
) -> None:
    """Point the module at stub stores returning one session on *host_id*."""
    conversations = SimpleNamespace(
        list_conversations_by_runner_id=lambda _rid: [SimpleNamespace(host_id=host_id)]
    )
    hosts = SimpleNamespace(get_host=lambda _hid: host)
    deployment = SimpleNamespace(
        for_provider=lambda _provider: SimpleNamespace(launcher_factory=lambda: launcher)
    )
    monkeypatch.setattr(managed_host_keepalive, "_conversation_store", conversations)
    monkeypatch.setattr(managed_host_keepalive, "_host_store", hosts)
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", deployment)


def _outcomes(caplog: pytest.LogCaptureFixture) -> list[str]:
    """The managed_keepalive outcome of every captured record, in order."""
    return [
        record.attributes["outcome"]
        for record in caplog.records
        if getattr(record, "attributes", {}).get("outcome")
    ]


_KEEPALIVE_LOGGER = "omnigent.server.managed_host_keepalive"


def test_extends_the_hosts_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = _Launcher()
    _wire(
        monkeypatch,
        launcher=launcher,
        host=SimpleNamespace(sandbox_id="sbx1", sandbox_provider="modal"),
    )
    managed_host_keepalive._keep_alive_for_runner("r1")
    assert launcher.calls == ["sbx1"]


def test_provider_without_keep_alive_is_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    # kubernetes today: the base class raises, and that must not propagate.
    launcher = _Launcher(raises=SandboxCapabilityError("nope"))
    _wire(
        monkeypatch,
        launcher=launcher,
        host=SimpleNamespace(sandbox_id="sbx1", sandbox_provider="kubernetes"),
    )
    with caplog.at_level(logging.DEBUG, logger=_KEEPALIVE_LOGGER):
        managed_host_keepalive._keep_alive_for_runner("r1")
    assert launcher.calls == ["sbx1"]  # attempted, error swallowed
    assert _outcomes(caplog) == ["unsupported"]


def test_store_failure_never_propagates(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def _boom(_rid: str) -> list[object]:
        raise RuntimeError("db down")

    monkeypatch.setattr(
        managed_host_keepalive,
        "_conversation_store",
        SimpleNamespace(list_conversations_by_runner_id=_boom),
    )
    monkeypatch.setattr(
        managed_host_keepalive, "_host_store", SimpleNamespace(get_host=lambda _h: None)
    )
    monkeypatch.setattr(
        managed_host_keepalive, "_sandbox_config", SimpleNamespace(for_provider=lambda _p: None)
    )
    with caplog.at_level(logging.WARNING, logger=_KEEPALIVE_LOGGER):
        managed_host_keepalive._keep_alive_for_runner("r1")  # must not raise
    assert _outcomes(caplog) == ["resolution_error"]
    assert caplog.records[0].attributes["error_type"] == "RuntimeError"
    assert "db down" not in caplog.text


def test_cli_host_without_a_sandbox_is_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    launcher = _Launcher()
    _wire(
        monkeypatch,
        launcher=launcher,
        host=SimpleNamespace(sandbox_id=None, sandbox_provider=None),
    )
    with caplog.at_level(logging.DEBUG, logger=_KEEPALIVE_LOGGER):
        managed_host_keepalive._keep_alive_for_runner("r1")
    assert launcher.calls == []
    assert _outcomes(caplog) == ["no_sandbox"]


def test_a_missing_host_row_is_recorded_and_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    launcher = _Launcher()
    _wire(monkeypatch, launcher=launcher, host=None)
    with caplog.at_level(logging.DEBUG, logger=_KEEPALIVE_LOGGER):
        managed_host_keepalive._keep_alive_for_runner("r1")
    assert launcher.calls == []
    assert _outcomes(caplog) == ["no_host"]


def test_touch_is_rate_limited_per_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    submitted: list[str] = []
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", object())
    monkeypatch.setattr(managed_host_keepalive, "_host_store", object())
    # touch() submits ctx.run(fn, runner_id), so the runner id is the last arg.
    monkeypatch.setattr(
        managed_host_keepalive,
        "_executor",
        SimpleNamespace(submit=lambda *args: submitted.append(args[-1])),
    )
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())

    managed_host_keepalive.touch("r1")
    managed_host_keepalive.touch("r1")  # inside the window: dropped
    managed_host_keepalive.touch("r2")  # different runner: allowed
    assert submitted == ["r1", "r2"]


def test_touch_is_a_noop_without_a_sandbox_config(monkeypatch: pytest.MonkeyPatch) -> None:
    submitted: list[str] = []
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", None)
    monkeypatch.setattr(
        managed_host_keepalive,
        "_executor",
        SimpleNamespace(submit=lambda *args: submitted.append(args)),
    )
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    managed_host_keepalive.touch("r1")
    assert submitted == []


def test_worker_runs_inside_the_callers_workspace_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The regression test for the real bug: the resolution chain reads stores that
    filter on `current_workspace_id()`, so the worker MUST inherit the caller's
    workspace ContextVar. A bare `submit` resolves it to the default workspace
    (0), matching no rows, and the sandbox is never extended.
    """
    from concurrent.futures import ThreadPoolExecutor

    from omnigent.db.db_models import current_workspace_id, workspace_scope

    seen: list[int] = []

    def _record(_rid: str) -> None:
        seen.append(current_workspace_id())

    monkeypatch.setattr(managed_host_keepalive, "_keep_alive_for_runner", _record)
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", object())
    monkeypatch.setattr(managed_host_keepalive, "_host_store", object())
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    with ThreadPoolExecutor(max_workers=1) as pool:
        monkeypatch.setattr(managed_host_keepalive, "_executor", pool)
        with workspace_scope(4242):
            managed_host_keepalive.touch("r1")
        pool.shutdown(wait=True)

    assert seen == [4242], "worker did not inherit the caller's workspace scope"


def test_a_host_on_an_unoffered_provider_is_skipped(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """
    Never extend through another provider's launcher. `recorded()` would fall
    back to the deployment default here, pushing a deadline on the wrong backend
    with a foreign sandbox id; `for_provider()` returns None and we skip.
    """
    launcher = _Launcher()
    conversations = SimpleNamespace(
        list_conversations_by_runner_id=lambda _rid: [SimpleNamespace(host_id="host1")]
    )
    hosts = SimpleNamespace(
        get_host=lambda _hid: SimpleNamespace(sandbox_id="sbx1", sandbox_provider="modal")
    )
    # Deployment no longer offers 'modal'. A default-returning resolver would
    # hand back some other provider's config; for_provider says None.
    deployment = SimpleNamespace(for_provider=lambda provider: None)
    monkeypatch.setattr(managed_host_keepalive, "_conversation_store", conversations)
    monkeypatch.setattr(managed_host_keepalive, "_host_store", hosts)
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", deployment)

    with caplog.at_level(logging.DEBUG, logger=_KEEPALIVE_LOGGER):
        managed_host_keepalive._keep_alive_for_runner("r1")
    assert launcher.calls == []
    assert _outcomes(caplog) == ["provider_unavailable"]


def test_a_runner_already_in_flight_is_not_queued_twice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled provider must not stack a second job for the same runner."""
    submitted: list[str] = []
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", object())
    monkeypatch.setattr(managed_host_keepalive, "_host_store", object())
    monkeypatch.setattr(
        managed_host_keepalive,
        "_executor",
        SimpleNamespace(submit=lambda *args: submitted.append(args[-1])),
    )
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())

    managed_host_keepalive.touch("r1")
    # Past the throttle window, but the first attempt has not finished.
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    managed_host_keepalive.touch("r1")
    assert submitted == ["r1"]

    # Once it clears, the next tick submits again.
    managed_host_keepalive._inflight.discard("r1")
    managed_host_keepalive.touch("r1")
    assert submitted == ["r1", "r1"]


def test_inflight_is_released_even_when_the_provider_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed attempt must not wedge the runner out of all future keepalives, and
    its record names the error type without the provider's message."""
    launcher = _Launcher(raises=RuntimeError("boom"))
    _wire(
        monkeypatch,
        launcher=launcher,
        host=SimpleNamespace(sandbox_id="sbx1", sandbox_provider="modal"),
    )
    monkeypatch.setattr(managed_host_keepalive, "_inflight", {"r1"})
    with caplog.at_level(logging.WARNING, logger=_KEEPALIVE_LOGGER):
        managed_host_keepalive._keep_alive_for_runner("r1")
    assert "r1" not in managed_host_keepalive._inflight
    assert _outcomes(caplog) == ["provider_error"]
    assert caplog.records[0].attributes["error_type"] == "RuntimeError"
    assert "boom" not in caplog.text


@pytest.mark.parametrize("failing_step", ["get_host", "for_provider"])
def test_one_hosts_resolution_failure_does_not_block_the_runners_other_hosts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, failing_step: str
) -> None:
    """A session spanning several hosts still refreshes the hosts that resolve."""
    launcher = _Launcher()
    hosts = {
        "host-bad": SimpleNamespace(sandbox_id="sbx-bad", sandbox_provider="broken"),
        "host-ok": SimpleNamespace(sandbox_id="sbx-ok", sandbox_provider="modal"),
    }

    def get_host(host_id: str) -> object:
        if failing_step == "get_host" and host_id == "host-bad":
            raise RuntimeError("row read failed: detail")
        return hosts[host_id]

    def for_provider(provider: str) -> object:
        if provider == "broken":
            raise RuntimeError("provider config failed: detail")
        return SimpleNamespace(launcher_factory=lambda: launcher)

    conversations = SimpleNamespace(
        list_conversations_by_runner_id=lambda _rid: [
            SimpleNamespace(host_id="host-bad"),
            SimpleNamespace(host_id="host-ok"),
        ]
    )
    monkeypatch.setattr(managed_host_keepalive, "_conversation_store", conversations)
    monkeypatch.setattr(managed_host_keepalive, "_host_store", SimpleNamespace(get_host=get_host))
    monkeypatch.setattr(
        managed_host_keepalive, "_sandbox_config", SimpleNamespace(for_provider=for_provider)
    )
    with caplog.at_level(logging.INFO, logger=_KEEPALIVE_LOGGER):
        managed_host_keepalive._keep_alive_for_runner("r1")
    assert launcher.calls == ["sbx-ok"]
    failure = "resolution_error" if failing_step == "get_host" else "provider_error"
    assert sorted(_outcomes(caplog)) == sorted([failure, "extended"])
    assert "detail" not in caplog.text


def test_keepalive_interval_is_provider_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    agent_sandbox refreshes fast (its window is short); other providers keep the
    cheap default so lowering agent_sandbox's cadence does not multiply their
    write load. An explicit env override wins for both.
    """
    from omnigent.onboarding.sandboxes.base import resolve_managed_keepalive_interval_s

    monkeypatch.delenv("OMNIGENT_MANAGED_KEEPALIVE_INTERVAL_S", raising=False)
    assert resolve_managed_keepalive_interval_s("agent_sandbox") == 60.0
    assert resolve_managed_keepalive_interval_s("modal") == 600.0
    assert resolve_managed_keepalive_interval_s() == 600.0
    monkeypatch.setenv("OMNIGENT_MANAGED_KEEPALIVE_INTERVAL_S", "15")
    assert resolve_managed_keepalive_interval_s("agent_sandbox") == 15.0
    assert resolve_managed_keepalive_interval_s("modal") == 15.0
    # A finite-but-huge override is clamped to the max, not passed through where
    # it would overflow the window-floor math (ceil(2 * interval)).
    monkeypatch.setenv("OMNIGENT_MANAGED_KEEPALIVE_INTERVAL_S", "1e308")
    assert resolve_managed_keepalive_interval_s("agent_sandbox") == 3600.0


def test_successful_keepalive_logs_at_info_on_the_server_logger(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """
    The keepalive INFO is emitted from the server layer (this module), whose
    logger surfaces in the server log — unlike the onboarding-layer launcher.
    """
    launcher = _Launcher()
    _wire(
        monkeypatch,
        launcher=launcher,
        host=SimpleNamespace(sandbox_id="sbx1", sandbox_provider="agent_sandbox"),
    )
    with caplog.at_level(logging.INFO, logger="omnigent.server.managed_host_keepalive"):
        managed_host_keepalive._keep_alive_for_runner("r1")
    assert any("kept managed sandbox sbx1 alive" in r.getMessage() for r in caplog.records)
    event = next(
        r for r in caplog.records if getattr(r, "attributes", {}).get("outcome") == "extended"
    )
    assert event.attributes["runner_id"] == "r1"
    assert event.attributes["host_id"] == "host1"
    assert event.attributes["provider"] == "agent_sandbox"


def test_soft_failed_keepalive_suppresses_the_success_info(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """
    When keep_alive returns False (attempted but not confirmed; the provider
    logged its own warning), the server loop must NOT log a success line, so the
    observability signal is never self-contradictory.
    """
    launcher = _Launcher(returns=False)
    _wire(
        monkeypatch,
        launcher=launcher,
        host=SimpleNamespace(sandbox_id="sbx1", sandbox_provider="agent_sandbox"),
    )
    with caplog.at_level(logging.INFO, logger="omnigent.server.managed_host_keepalive"):
        managed_host_keepalive._keep_alive_for_runner("r1")
    assert launcher.calls == ["sbx1"]  # attempted
    assert not any("kept managed sandbox" in r.getMessage() for r in caplog.records)
    event = next(
        r for r in caplog.records if getattr(r, "attributes", {}).get("outcome") == "soft_failed"
    )
    assert event.attributes["runner_id"] == "r1"
    assert event.attributes["host_id"] == "host1"
    assert event.attributes["provider"] == "agent_sandbox"
    assert event.attributes["error_type"] == "soft_failure"


def test_keepalive_interval_caches_the_runners_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Before the runner's provider is known, the loop/throttle use the fast
    agent_sandbox cadence (never under-refresh a short window); once
    _keep_alive_for_runner resolves the provider, the runner's own cadence is
    cached and returned.
    """
    monkeypatch.delenv("OMNIGENT_MANAGED_KEEPALIVE_INTERVAL_S", raising=False)
    monkeypatch.setattr(managed_host_keepalive, "_runner_interval_s", {})
    # unknown runner -> fast agent_sandbox default, so a short window is safe
    assert managed_host_keepalive.keepalive_interval_s("r1") == 60.0
    _wire(
        monkeypatch,
        launcher=_Launcher(),
        host=SimpleNamespace(sandbox_id="sbx1", sandbox_provider="modal"),
    )
    managed_host_keepalive._keep_alive_for_runner("r1")
    # now cached at modal's slower cadence
    assert managed_host_keepalive.keepalive_interval_s("r1") == 600.0


_HOST_OF = {"runner-a": "host-a", "runner-b": "host-b"}
_HOSTS = {
    "host-a": SimpleNamespace(sandbox_id="sbx-a", sandbox_provider="modal"),
    "host-b": SimpleNamespace(sandbox_id="sbx-b", sandbox_provider="modal"),
}


class _GatedLauncher:
    """Records when each sandbox's keep_alive starts; can hold chosen sandboxes' calls open."""

    def __init__(
        self,
        *,
        block: str | set[str] | None = None,
        block_once: bool = False,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.calls: list[tuple[str, float]] = []
        self.release = threading.Event()
        self._block = {block} if isinstance(block, str) else set(block or ())
        self._block_once = block_once
        self._blocked = False
        self._clock = clock
        self._started: dict[str, threading.Event] = {}
        self._lock = threading.Lock()

    def started(self, sandbox_id: str) -> threading.Event:
        with self._lock:
            return self._started.setdefault(sandbox_id, threading.Event())

    def keep_alive(self, sandbox_id: str) -> None:
        self.calls.append((sandbox_id, self._clock()))
        self.started(sandbox_id).set()
        if sandbox_id in self._block and not (self._block_once and self._blocked):
            self._blocked = True
            self.release.wait(10)


def _wire_runners(
    monkeypatch: pytest.MonkeyPatch, launcher: _GatedLauncher, *, executor: object | None
) -> tuple[Any, Any, Any]:
    """Resolve each runner in _HOST_OF to its own managed sandbox, with fresh scheduler state."""
    conversations = SimpleNamespace(
        list_conversations_by_runner_id=lambda rid: [SimpleNamespace(host_id=_HOST_OF[rid])]
    )
    hosts = SimpleNamespace(get_host=lambda hid: _HOSTS[hid])
    deployment = SimpleNamespace(
        for_provider=lambda _provider: SimpleNamespace(launcher_factory=lambda: launcher)
    )
    monkeypatch.setattr(managed_host_keepalive, "_conversation_store", conversations)
    monkeypatch.setattr(managed_host_keepalive, "_host_store", hosts)
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", deployment)
    monkeypatch.setattr(managed_host_keepalive, "_executor", executor)
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    monkeypatch.setattr(managed_host_keepalive, "_runner_interval_s", {})
    return conversations, hosts, deployment


def _configure_real_executor(
    monkeypatch: pytest.MonkeyPatch, launcher: _GatedLauncher
) -> ThreadPoolExecutor:
    """Wire the stubs through configure() so the executor is the one production builds."""
    stores = _wire_runners(monkeypatch, launcher, executor=None)
    managed_host_keepalive.configure(*stores)
    executor = managed_host_keepalive._executor
    assert isinstance(executor, ThreadPoolExecutor)
    return executor


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now


class _SteppedSleep:
    """Stands in for asyncio inside runner_tunnel: each sleep records its delay and parks
    until the test releases it, then advances the fake clock to the wake time."""

    def __init__(self, clock: _FakeClock) -> None:
        self._clock = clock
        self.sleeps: list[tuple[float, float]] = []
        self.parked = asyncio.Event()
        self._release = asyncio.Event()

    async def sleep(self, delay: float) -> None:
        self.sleeps.append((self._clock.now, delay))
        wake_at = self._clock.now + delay
        self.parked.set()
        await self._release.wait()
        self._release.clear()
        self._clock.now = max(self._clock.now, wake_at)

    async def next_tick(self) -> None:
        """Wake the loop from its current sleep and wait for it to park at the next one."""
        self.parked.clear()
        self._release.set()
        await self.parked.wait()

    def __getattr__(self, name: str) -> Any:
        return getattr(asyncio, name)


class _DeferredExecutor:
    """Holds submitted keepalive jobs so the test decides when a worker runs each one."""

    def __init__(self, clock: _FakeClock) -> None:
        self._clock = clock
        self.submitted_at: list[float] = []
        self._queued: list[tuple[Future[Any], Callable[..., Any], tuple[Any, ...]]] = []

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future[Any]:
        future: Future[Any] = Future()
        self.submitted_at.append(self._clock.now)
        self._queued.append((future, fn, args))
        return future

    def run_queued(self) -> None:
        while self._queued:
            future, fn, args = self._queued.pop(0)
            future.set_running_or_notify_cancel()
            try:
                future.set_result(fn(*args))
            except BaseException as exc:
                future.set_exception(exc)


def _freeze_scheduler_clock(
    monkeypatch: pytest.MonkeyPatch, interval: float
) -> tuple[_FakeClock, _SteppedSleep]:
    """Pin the cadence and route the throttle clock and the tunnel loop's sleep through fakes."""
    monkeypatch.setenv(MANAGED_KEEPALIVE_INTERVAL_ENV_VAR, str(interval))
    clock = _FakeClock()
    monkeypatch.setattr(managed_host_keepalive, "time", clock)
    stepper = _SteppedSleep(clock)
    monkeypatch.setattr(runner_tunnel, "asyncio", stepper)
    return clock, stepper


async def _stop_loop(task: asyncio.Task[None]) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _wait_until(condition: Callable[[], bool], *, timeout: float = 5.0) -> None:
    """Poll a worker-thread side effect from the test's event loop."""
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, "worker did not reach the expected state"
        await asyncio.sleep(0.01)


def test_a_blocked_provider_call_does_not_starve_an_unrelated_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One runner's stalled keep_alive must not delay another runner's due refresh."""
    launcher = _GatedLauncher(block="sbx-a")
    executor = _configure_real_executor(monkeypatch, launcher)
    try:
        managed_host_keepalive.touch("runner-a")
        assert launcher.started("sbx-a").wait(5), "blocked runner never reached the provider"
        managed_host_keepalive.touch("runner-b")
        unrelated_started = launcher.started("sbx-b").wait(2)
    finally:
        launcher.release.set()
        executor.shutdown(wait=True)

    assert {sandbox_id for sandbox_id, _ in launcher.calls} == {"sbx-a", "sbx-b"}
    assert unrelated_started, (
        "runner-b's keep_alive did not start while runner-a's call was blocked "
        f"(executor max_workers={executor._max_workers}); calls={launcher.calls}"
    )


async def test_a_slow_provider_call_does_not_push_the_next_refresh_out_a_full_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A refresh still running when the next one is due is retried soon after it
    finishes, so two provider calls are never a whole extra cadence apart."""
    interval, overrun = 60.0, 10.0
    clock, stepper = _freeze_scheduler_clock(monkeypatch, interval)
    launcher = _GatedLauncher(block="sbx-b", block_once=True, clock=clock.monotonic)
    executor = _configure_real_executor(monkeypatch, launcher)

    task = asyncio.create_task(runner_tunnel._keepalive_loop("runner-b"))
    try:
        await stepper.parked.wait()
        assert await asyncio.to_thread(launcher.started("sbx-b").wait, 5)
        while clock.now < interval:
            await stepper.next_tick()
        assert [sandbox_id for sandbox_id, _ in launcher.calls] == ["sbx-b"], (
            "a second attempt was queued behind the running one"
        )
        clock.now = interval + overrun
        launcher.release.set()
        await _wait_until(lambda: "runner-b" not in managed_host_keepalive._inflight)
        await stepper.next_tick()
        await _wait_until(lambda: len(launcher.calls) >= 2)
    finally:
        launcher.release.set()
        await _stop_loop(task)
        executor.shutdown(wait=True)

    (_, first_at), (_, second_at) = launcher.calls[:2]
    assert second_at - first_at < 2 * interval, (
        f"the refresh after a {overrun:.0f}s provider overrun waited until t={second_at}; "
        f"calls={launcher.calls}, loop sleeps={stepper.sleeps}"
    )


async def test_a_tick_rejected_while_an_attempt_is_outstanding_is_retried_before_the_next_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected due tick must wake again from the remaining due time, not a full cadence."""
    interval, delay = 60.0, 1.0
    clock, stepper = _freeze_scheduler_clock(monkeypatch, interval)
    launcher = _GatedLauncher(clock=clock.monotonic)
    executor = _DeferredExecutor(clock)
    _wire_runners(monkeypatch, launcher, executor=executor)

    task = asyncio.create_task(runner_tunnel._keepalive_loop("runner-b"))
    try:
        await stepper.parked.wait()
        assert executor.submitted_at == [0.0]
        await stepper.next_tick()
        assert executor.submitted_at == [0.0], "a second attempt was queued behind the first"
        clock.now = interval + delay
        executor.run_queued()
        assert [sandbox_id for sandbox_id, _ in launcher.calls] == ["sbx-b"]
        await stepper.next_tick()
    finally:
        await _stop_loop(task)

    assert len(executor.submitted_at) == 2, f"no retry after the rejected tick: {stepper.sleeps}"
    gap = executor.submitted_at[1] - executor.submitted_at[0]
    assert gap < 2 * interval, (
        f"refresh attempts {executor.submitted_at} are a full cadence apart after the tick "
        f"at t={interval} was rejected and the worker freed at t={interval + delay}; "
        f"loop sleeps={stepper.sleeps}"
    )


async def test_a_small_start_delay_does_not_throttle_the_next_on_cadence_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A worker starting shortly after submission must not push the next refresh out a cadence."""
    interval, delay = 60.0, 1.0
    clock, stepper = _freeze_scheduler_clock(monkeypatch, interval)
    launcher = _GatedLauncher(clock=clock.monotonic)
    executor = _DeferredExecutor(clock)
    _wire_runners(monkeypatch, launcher, executor=executor)

    task = asyncio.create_task(runner_tunnel._keepalive_loop("runner-b"))
    try:
        await stepper.parked.wait()
        clock.now = delay
        executor.run_queued()
        await stepper.next_tick()
    finally:
        await _stop_loop(task)

    assert executor.submitted_at == [0.0, interval], f"loop sleeps={stepper.sleeps}"


class _CountingExecutor:
    """Delegates to a real executor while recording when each submission was attempted."""

    def __init__(self, inner: ThreadPoolExecutor, clock: _FakeClock) -> None:
        self._inner = inner
        self._clock = clock
        self.attempts: list[float] = []

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future[Any]:
        self.attempts.append(self._clock.now)
        return self._inner.submit(fn, *args)


async def test_a_rejected_submission_is_retried_with_a_bounded_delay(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When the pool rejects a due refresh, later ticks must retry it after a bounded delay."""
    interval = 60.0
    clock, stepper = _freeze_scheduler_clock(monkeypatch, interval)
    launcher = _GatedLauncher(clock=clock.monotonic)
    pool = _configure_real_executor(monkeypatch, launcher)
    pool.shutdown(wait=True)
    executor = _CountingExecutor(pool, clock)
    monkeypatch.setattr(managed_host_keepalive, "_executor", executor)

    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(runner_tunnel._keepalive_loop("runner-b"))
        try:
            await stepper.parked.wait()
            for _ in range(3):
                await stepper.next_tick()
        finally:
            await _stop_loop(task)

    sleeps = [delay for _, delay in stepper.sleeps]
    # The scheduler's own records plus the tunnel loop's, which logs an ERROR if
    # touch() raises; unrelated libraries must not count.
    records = [record for record in caplog.records if record.name.startswith("omnigent.server")]
    observed = {
        "attempts": executor.attempts,
        "sleeps": sleeps,
        "inflight": sorted(managed_host_keepalive._inflight),
        "logs": [record.getMessage() for record in records],
    }
    assert all(0 < delay <= interval for delay in sleeps), observed
    assert len(records) <= len(executor.attempts), observed
    assert "runner-b" not in managed_host_keepalive._inflight, (
        f"rejected submission left the runner reserved, so it can never refresh again: {observed}"
    )
    assert len(executor.attempts) >= 2, f"rejected submission was never retried: {observed}"
    # Paced by the interval: one attempt and one record per cadence, no faster.
    assert executor.attempts == [0.0, interval, 2 * interval, 3 * interval], observed
    assert sleeps == [interval] * 4, observed
    assert _outcomes(caplog) == ["submission_failed"] * 4, observed


def test_pruning_keeps_the_stamp_of_a_runner_with_an_outstanding_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A long-stalled attempt must not lose its stamp to pruning, or the loop would
    wait a full interval instead of retrying shortly once the call clears."""
    monkeypatch.delenv(MANAGED_KEEPALIVE_INTERVAL_ENV_VAR, raising=False)
    clock = _FakeClock()
    clock.now = 2000.0
    monkeypatch.setattr(managed_host_keepalive, "time", clock)
    monkeypatch.setattr(managed_host_keepalive, "_THROTTLE_MAX_ENTRIES", 2)
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", object())
    monkeypatch.setattr(managed_host_keepalive, "_host_store", object())
    monkeypatch.setattr(
        managed_host_keepalive, "_executor", SimpleNamespace(submit=lambda *_: None)
    )
    monkeypatch.setattr(managed_host_keepalive, "_runner_interval_s", {})
    monkeypatch.setattr(
        managed_host_keepalive, "_last_kept", {"stuck": 0.0, "gone-1": 0.0, "gone-2": 0.0}
    )
    monkeypatch.setattr(managed_host_keepalive, "_inflight", {"stuck"})

    managed_host_keepalive.touch("fresh")  # grows the map past the cap: prune runs

    assert set(managed_host_keepalive._last_kept) == {"stuck", "fresh"}
    assert managed_host_keepalive.next_keepalive_delay_s("stuck", now=clock.now) == 1.0


def test_submission_failure_releases_the_runner_and_paces_the_retry(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A rejected submit is recorded without the pool's message and counts as the attempt,
    so the runner is neither wedged nor retried faster than its cadence."""
    clock = _FakeClock()
    clock.now = 100.0

    class _RejectingExecutor:
        def submit(self, *_args: object) -> None:
            raise RuntimeError("cannot schedule: pool-internal-detail")

    monkeypatch.setattr(managed_host_keepalive, "time", clock)
    monkeypatch.setattr(managed_host_keepalive, "_executor", _RejectingExecutor())
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", object())
    monkeypatch.setattr(managed_host_keepalive, "_host_store", object())
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_runner_interval_s", {"r1": 60.0})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())

    with caplog.at_level(logging.WARNING, logger="omnigent.server.managed_host_keepalive"):
        managed_host_keepalive.touch("r1")
    assert "r1" not in managed_host_keepalive._inflight
    event = next(
        record
        for record in caplog.records
        if getattr(record, "attributes", {}).get("outcome") == "submission_failed"
    )
    assert event.attributes["error_type"] == "RuntimeError"
    assert "pool-internal-detail" not in event.getMessage()
    assert managed_host_keepalive.next_keepalive_delay_s("r1", now=clock.now) == 60.0

    submitted: list[str] = []
    monkeypatch.setattr(
        managed_host_keepalive,
        "_executor",
        SimpleNamespace(submit=lambda *args: submitted.append(args[-1])),
    )
    managed_host_keepalive.touch("r1")  # inside the window: not retried early
    assert submitted == []
    clock.now += 60.0
    managed_host_keepalive.touch("r1")
    assert submitted == ["r1"]


def test_next_delay_wakes_at_the_remaining_due_time_and_retries_when_overdue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A due tick cannot defer the next refresh by another full interval."""
    monkeypatch.setattr(managed_host_keepalive, "_runner_interval_s", {"r1": 60.0})
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {"r1": 0.005})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", object())
    monkeypatch.setattr(managed_host_keepalive, "_host_store", object())
    monkeypatch.setattr(managed_host_keepalive, "_executor", object())

    # The loop woke 4 ms before the attempt is due: sleep just the remainder.
    assert managed_host_keepalive.next_keepalive_delay_s("r1", now=60.001) == pytest.approx(0.004)

    # Due, but the attempt is still queued or running: retry shortly rather
    # than sleeping another 60s and crossing a 2x-interval shutdown window.
    monkeypatch.setattr(managed_host_keepalive, "_inflight", {"r1"})
    assert managed_host_keepalive.next_keepalive_delay_s("r1", now=60.005) == 1.0

    # Due with nothing outstanding (it cleared right after this tick's touch):
    # the same bounded retry, never a zero delay that would spin the loop.
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    assert managed_host_keepalive.next_keepalive_delay_s("r1", now=60.005) == 1.0


def test_next_delay_uses_the_interval_when_managed_keepalive_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale throttle state cannot make a disabled keepalive loop spin."""
    monkeypatch.setattr(managed_host_keepalive, "_runner_interval_s", {"r1": 60.0})
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {"r1": 0.005})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", None)
    monkeypatch.setattr(managed_host_keepalive, "_host_store", None)
    monkeypatch.setattr(managed_host_keepalive, "_executor", None)

    assert managed_host_keepalive.next_keepalive_delay_s("r1", now=60.005) == 60.0


def test_saturated_pool_keeps_a_queued_runner_single_flight(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A runner queued behind a saturated pool holds one reservation, runs once capacity
    returns with its wait recorded as queue delay, and is then throttled like any other
    refresh: no submission burst."""
    workers = managed_host_keepalive._KEEPALIVE_MAX_WORKERS
    busy = [f"busy-{index}" for index in range(workers)]
    launcher = _GatedLauncher(block={f"sbx-{runner_id}" for runner_id in busy})
    conversations = SimpleNamespace(
        list_conversations_by_runner_id=lambda rid: [SimpleNamespace(host_id=rid)]
    )
    hosts = SimpleNamespace(
        get_host=lambda hid: SimpleNamespace(sandbox_id=f"sbx-{hid}", sandbox_provider="modal")
    )
    deployment = SimpleNamespace(
        for_provider=lambda _provider: SimpleNamespace(launcher_factory=lambda: launcher)
    )
    pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="test-managed-keepalive")
    submitted: list[str] = []

    class _RecordingPool:
        def submit(self, *args: Any) -> Future[None]:
            submitted.append(args[-1])
            return pool.submit(*args)

    monkeypatch.setattr(managed_host_keepalive, "_conversation_store", conversations)
    monkeypatch.setattr(managed_host_keepalive, "_host_store", hosts)
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", deployment)
    monkeypatch.setattr(managed_host_keepalive, "_executor", _RecordingPool())
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_runner_interval_s", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    queued_wait = 0.02
    try:
        for runner_id in busy:
            managed_host_keepalive.touch(runner_id)
        assert all(launcher.started(f"sbx-{runner_id}").wait(1.0) for runner_id in busy)

        caplog.set_level(logging.INFO, logger=_KEEPALIVE_LOGGER)
        for _ in range(6):
            managed_host_keepalive.touch("queued")
        assert submitted.count("queued") == 1
        with managed_host_keepalive._state_lock:
            assert "queued" in managed_host_keepalive._inflight
        assert not launcher.started("sbx-queued").is_set()

        time.sleep(queued_wait)
        launcher.release.set()
        assert launcher.started("sbx-queued").wait(1.0)
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            with managed_host_keepalive._state_lock:
                if "queued" not in managed_host_keepalive._inflight:
                    break
            time.sleep(0.001)
        with managed_host_keepalive._state_lock:
            assert "queued" not in managed_host_keepalive._inflight

        for _ in range(5):
            managed_host_keepalive.touch("queued")
        assert [sandbox_id for sandbox_id, _ in launcher.calls].count("sbx-queued") == 1
        assert submitted.count("queued") == 1
    finally:
        launcher.release.set()
        pool.shutdown(wait=True)

    extended = next(
        record
        for record in caplog.records
        if getattr(record, "attributes", {}).get("sandbox_id") == "sbx-queued"
    )
    assert extended.attributes["outcome"] == "extended"
    assert extended.attributes["queue_delay_s"] >= queued_wait
    assert extended.attributes["provider_duration_s"] >= 0
