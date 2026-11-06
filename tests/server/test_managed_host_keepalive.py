"""Tests for the managed-path sandbox keepalive.

Covers the resolution chain (runner -> session -> host -> provider), the
per-runner rate limit, and the two skip paths (provider can't extend, host has
no sandbox). Stubs stand in for the stores/deployment: the module only reads a
few attributes off each, so a real store would add setup without adding cover.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

from omnigent.onboarding.sandboxes.base import SandboxCapabilityError
from omnigent.server import managed_host_keepalive


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


def test_extends_the_hosts_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = _Launcher()
    _wire(
        monkeypatch,
        launcher=launcher,
        host=SimpleNamespace(user_id="alice", sandbox_id="sbx1", sandbox_provider="modal"),
    )
    managed_host_keepalive._keep_alive_for_runner("r1")
    assert launcher.calls == ["sbx1"]


def test_provider_without_keep_alive_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    # kubernetes today: the base class raises, and that must not propagate.
    launcher = _Launcher(raises=SandboxCapabilityError("nope"))
    _wire(
        monkeypatch,
        launcher=launcher,
        host=SimpleNamespace(user_id="alice", sandbox_id="sbx1", sandbox_provider="kubernetes"),
    )
    managed_host_keepalive._keep_alive_for_runner("r1")
    assert launcher.calls == ["sbx1"]  # attempted, error swallowed


def test_store_failure_never_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
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
    managed_host_keepalive._keep_alive_for_runner("r1")  # must not raise


def test_cli_host_without_a_sandbox_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = _Launcher()
    _wire(
        monkeypatch,
        launcher=launcher,
        host=SimpleNamespace(sandbox_id=None, sandbox_provider=None),
    )
    managed_host_keepalive._keep_alive_for_runner("r1")
    assert launcher.calls == []


def test_touch_is_rate_limited_per_runner(monkeypatch: pytest.MonkeyPatch) -> None:
    submitted: list[str] = []
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", object())
    monkeypatch.setattr(managed_host_keepalive, "_host_store", object())
    # touch() now submits ctx.run(fn, runner_id), so the runner id is the last arg.
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
    monkeypatch: pytest.MonkeyPatch,
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
        get_host=lambda _hid: SimpleNamespace(
            user_id="alice", sandbox_id="sbx1", sandbox_provider="modal"
        )
    )
    # Deployment no longer offers 'modal'. A default-returning resolver would
    # hand back some other provider's config; for_provider says None.
    deployment = SimpleNamespace(for_provider=lambda provider: None)
    monkeypatch.setattr(managed_host_keepalive, "_conversation_store", conversations)
    monkeypatch.setattr(managed_host_keepalive, "_host_store", hosts)
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", deployment)

    managed_host_keepalive._keep_alive_for_runner("r1")
    assert launcher.calls == []


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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed attempt must not wedge the runner out of all future keepalives."""
    launcher = _Launcher(raises=RuntimeError("boom"))
    _wire(
        monkeypatch,
        launcher=launcher,
        host=SimpleNamespace(user_id="alice", sandbox_id="sbx1", sandbox_provider="modal"),
    )
    monkeypatch.setattr(managed_host_keepalive, "_inflight", {"r1"})
    managed_host_keepalive._keep_alive_for_runner("r1")
    assert "r1" not in managed_host_keepalive._inflight


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
        host=SimpleNamespace(user_id="alice", sandbox_id="sbx1", sandbox_provider="agent_sandbox"),
    )
    with caplog.at_level(logging.INFO, logger="omnigent.server.managed_host_keepalive"):
        managed_host_keepalive._keep_alive_for_runner("r1")
    assert any("kept managed sandbox sbx1 alive" in r.getMessage() for r in caplog.records)


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
        host=SimpleNamespace(user_id="alice", sandbox_id="sbx1", sandbox_provider="agent_sandbox"),
    )
    with caplog.at_level(logging.INFO, logger="omnigent.server.managed_host_keepalive"):
        managed_host_keepalive._keep_alive_for_runner("r1")
    assert launcher.calls == ["sbx1"]  # attempted
    assert not any("kept managed sandbox" in r.getMessage() for r in caplog.records)


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
        host=SimpleNamespace(user_id="alice", sandbox_id="sbx1", sandbox_provider="modal"),
    )
    managed_host_keepalive._keep_alive_for_runner("r1")
    # now cached at modal's slower cadence
    assert managed_host_keepalive.keepalive_interval_s("r1") == 600.0


def test_owner_identity_launcher_is_bound_to_the_hosts_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An owner-identity launcher (e.g. Databricks Sandboxes run as their owner)
    # refreshes with the host owner's credential, never a server identity.
    class _OwnerLauncher(_Launcher):
        owner_credential_provider = "databricks"

        def __init__(self) -> None:
            super().__init__()
            self.bound: list[object] = []

        def bind_owner_credential(self, resolve: object) -> None:
            self.bound.append(resolve)

    launcher = _OwnerLauncher()
    seen: list[str] = []
    _wire(
        monkeypatch,
        launcher=launcher,
        host=SimpleNamespace(user_id="alice", sandbox_id="sbx1", sandbox_provider="databricks"),
    )
    managed_host_keepalive._sandbox_config.owner_credentials = {  # type: ignore[union-attr]
        "databricks": lambda user: seen.append(user)
    }

    managed_host_keepalive._keep_alive_for_runner("r1")

    assert launcher.calls == ["sbx1"]
    (resolve,) = launcher.bound
    resolve()  # type: ignore[operator]
    assert seen == ["alice"]


class _BusyGate:
    """Drive touch()/record_pong() with a fake clock and a recording executor."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.submitted: list[str] = []
        self.now = 1000.0
        monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", object())
        monkeypatch.setattr(managed_host_keepalive, "_host_store", object())
        monkeypatch.setattr(
            managed_host_keepalive, "_executor", SimpleNamespace(submit=self._submit)
        )
        for name in (
            "_last_kept",
            "_runner_interval_s",
            "_last_pong_busy",
            "_busy_since_kept",
            "_last_busy_at",
            "_runner_warm_grace_s",
            "_last_report_at",
        ):
            monkeypatch.setattr(managed_host_keepalive, name, {})
        monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
        monkeypatch.setattr(managed_host_keepalive, "_interval_for", lambda _runner_id: 600.0)
        monkeypatch.setattr(managed_host_keepalive.time, "monotonic", lambda: self.now)

    def _submit(self, *args: object) -> None:
        # touch() submits ctx.run(fn, runner_id); the real job frees the in-flight slot.
        runner_id = str(args[-1])
        self.submitted.append(runner_id)
        managed_host_keepalive._inflight.discard(runner_id)

    def tick(self, runner_id: str = "r1") -> None:
        """One _keepalive_loop iteration, a full interval after the last."""
        self.now += 600.0
        managed_host_keepalive.touch(runner_id)


def test_runner_that_never_reports_busy_keeps_tunnel_lifetime_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _BusyGate(monkeypatch)
    managed_host_keepalive.touch("r1")
    gate.tick()
    gate.tick()
    assert gate.submitted == ["r1", "r1", "r1"]


def test_idle_runner_is_not_refreshed_until_work_resumes(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _BusyGate(monkeypatch)
    managed_host_keepalive.touch("r1")  # connect: nothing reported yet
    managed_host_keepalive.record_pong("r1", False)
    gate.tick()
    gate.tick()
    assert gate.submitted == ["r1"], "an idle runner must let its sandbox stop"

    # The first busy pong after idle refreshes at once, not at the next tick.
    gate.now += 30.0
    managed_host_keepalive.record_pong("r1", True)
    assert gate.submitted == ["r1", "r1"]


def test_busy_runner_is_refreshed_once_per_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _BusyGate(monkeypatch)
    managed_host_keepalive.touch("r1")
    for _ in range(3):
        managed_host_keepalive.record_pong("r1", True)
        gate.tick()
    assert gate.submitted == ["r1"] * 4

    managed_host_keepalive.record_pong("r1", True)
    managed_host_keepalive.record_pong("r1", True)
    assert len(gate.submitted) == 4, "busy pongs inside an interval must not refresh on their own"


def test_busy_runner_is_refreshed_on_every_tick_between_pongs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 5s refresh interval with 30s pongs: a runner still busy at its last pong
    # must be refreshed on every tick until a pong says otherwise, or a short
    # platform window expires between pongs.
    gate = _BusyGate(monkeypatch)
    monkeypatch.setattr(managed_host_keepalive, "_interval_for", lambda _runner_id: 5.0)
    managed_host_keepalive.touch("r1")
    managed_host_keepalive.record_pong("r1", True)
    for _ in range(5):
        gate.now += 5.0
        managed_host_keepalive.touch("r1")
    assert gate.submitted == ["r1"] * 6

    managed_host_keepalive.record_pong("r1", False)
    gate.now += 5.0
    managed_host_keepalive.touch("r1")
    gate.now += 5.0
    managed_host_keepalive.touch("r1")
    assert len(gate.submitted) == 6, "an idle pong stops refreshes after its pending one"


def test_pongs_record_nothing_without_managed_keepalive(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _BusyGate(monkeypatch)
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", None)
    managed_host_keepalive.record_pong("r1", True)
    managed_host_keepalive.record_pong("r1", False)
    assert managed_host_keepalive._last_pong_busy == {}
    assert managed_host_keepalive._busy_since_kept == {}
    assert gate.submitted == []


def test_work_that_ended_inside_an_interval_is_refreshed_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    gate = _BusyGate(monkeypatch)
    managed_host_keepalive.touch("r1")
    managed_host_keepalive.record_pong("r1", True)
    managed_host_keepalive.record_pong("r1", False)
    gate.tick()
    gate.tick()
    assert gate.submitted == ["r1", "r1"]


def test_unreported_pong_changes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _BusyGate(monkeypatch)
    managed_host_keepalive.record_pong("r1", None)
    assert gate.submitted == []
    assert "r1" not in managed_host_keepalive._busy_since_kept


def test_forced_refresh_keeps_the_periodic_schedule(monkeypatch: pytest.MonkeyPatch) -> None:
    # An idle-to-busy refresh between ticks must not push the next periodic
    # refresh a whole interval later.
    gate = _BusyGate(monkeypatch)
    managed_host_keepalive.touch("r1")  # t=1000
    managed_host_keepalive.record_pong("r1", False)
    gate.now += 61.0
    managed_host_keepalive.record_pong("r1", True)  # forced at t=1061
    gate.now += 539.0
    managed_host_keepalive.touch("r1")  # periodic tick at t=1600
    assert gate.submitted == ["r1", "r1", "r1"]


def test_dispatched_work_refreshes_before_the_next_pong(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = _BusyGate(monkeypatch)
    managed_host_keepalive.touch("r1")
    managed_host_keepalive.record_pong("r1", False)
    gate.now += 1.0
    managed_host_keepalive.record_work_dispatched("r1")  # a message right after an idle pong
    assert gate.submitted == ["r1", "r1"], "new work must not wait for the next pong"
    # It counts as busy until the runner's next pong says otherwise.
    gate.tick()
    assert gate.submitted == ["r1"] * 3


def test_runner_stays_warm_for_its_grace_after_the_last_busy_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # agent_sandbox: keep_warm_s (here 1500s) after the runner stops being busy.
    gate = _BusyGate(monkeypatch)
    managed_host_keepalive._runner_warm_grace_s["r1"] = 1500.0
    managed_host_keepalive.touch("r1")
    managed_host_keepalive.record_pong("r1", True)  # last busy at t=1000
    managed_host_keepalive.record_pong("r1", False)
    gate.tick()  # t=1600: refreshed for the busy interval
    gate.tick()  # t=2200: idle, but within the grace
    gate.tick()  # t=2800: grace over
    assert gate.submitted == ["r1", "r1", "r1"]


def test_pruning_preserves_live_idle_reporting_state(monkeypatch: pytest.MonkeyPatch) -> None:
    # An idle reporting runner is no longer refreshed, so its refresh time goes
    # stale while its pongs keep coming; pruning must not turn it back into a
    # never-reporting runner that gets refreshed.
    gate = _BusyGate(monkeypatch)
    managed_host_keepalive.touch("r1")
    managed_host_keepalive.record_pong("r1", False)
    gate.now += 5000.0
    managed_host_keepalive.record_pong("r1", False)  # still connected and reporting
    managed_host_keepalive._prune_throttle(gate.now)
    managed_host_keepalive.touch("r1")
    assert gate.submitted == ["r1"]


def test_an_idle_runner_stays_idle_across_a_reconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    # The new tunnel's immediate first touch must not refresh a runner that
    # reported idle, or every tunnel reconnect would extend its sandbox.
    gate = _BusyGate(monkeypatch)
    managed_host_keepalive.touch("r1")
    managed_host_keepalive.record_pong("r1", False)
    gate.now += 900.0
    managed_host_keepalive.touch("r1")  # reconnect: _keepalive_loop's first touch
    assert gate.submitted == ["r1"]


def test_a_runner_that_stops_reporting_gets_tunnel_lifetime_refreshes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An older runner build may reconnect under the same id; its pongs carry no busy.
    gate = _BusyGate(monkeypatch)
    managed_host_keepalive.touch("r1")
    managed_host_keepalive.record_pong("r1", False)
    managed_host_keepalive.record_pong("r1", None)
    gate.tick()
    assert gate.submitted == ["r1", "r1"]
