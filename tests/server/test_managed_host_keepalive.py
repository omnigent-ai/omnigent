"""Tests for the managed-path sandbox keepalive.

Covers the resolution chain (runner -> session -> host -> provider), the
per-runner rate limit, and the two skip paths (provider can't extend, host has
no sandbox). Stubs stand in for the stores/deployment: the module only reads a
few attributes off each, so a real store would add setup without adding cover.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from omnigent.onboarding.sandboxes.base import SandboxCapabilityError
from omnigent.server import managed_host_keepalive
from tests.budgets import budget


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


def _wire_recording_executor(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Enable the keepalive with an executor that records submissions."""
    submitted: list[str] = []

    def _submit(*args: object) -> None:
        # touch() submits ctx.run(fn, runner_id); the real job frees the in-flight slot.
        runner_id = str(args[-1])
        submitted.append(runner_id)
        managed_host_keepalive._inflight.discard(runner_id)

    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", object())
    monkeypatch.setattr(managed_host_keepalive, "_host_store", object())
    monkeypatch.setattr(managed_host_keepalive, "_executor", SimpleNamespace(submit=_submit))
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    monkeypatch.setattr(managed_host_keepalive, "_reports", {})
    return submitted


@pytest.mark.parametrize(
    ("keep_warm", "refreshed"),
    [(True, True), (False, False), (None, True)],
    ids=["keep-warm", "let-it-stop", "older-runner"],
)
def test_touch_follows_the_runners_latest_report(
    monkeypatch: pytest.MonkeyPatch, keep_warm: bool | None, refreshed: bool
) -> None:
    submitted = _wire_recording_executor(monkeypatch)
    managed_host_keepalive.open_report("r1")
    assert managed_host_keepalive.touch("r1") is False, "no refresh before the first pong"
    managed_host_keepalive.record_pong("r1", keep_warm)
    assert managed_host_keepalive.touch("r1") is refreshed
    assert submitted == (["r1"] if refreshed else [])


def test_a_pong_wakes_the_loop_only_when_a_refresh_becomes_due(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire_recording_executor(monkeypatch)
    report = managed_host_keepalive.open_report("r1")
    for keep_warm, woken in (
        (False, True),  # the first pong: the loop can now decide
        (False, False),
        (True, True),  # work resumed
        (True, False),
        (None, False),  # still refreshing
        (False, False),
        (None, True),  # an older runner took over the id
    ):
        report.wake.clear()
        managed_host_keepalive.record_pong("r1", keep_warm)
        assert report.wake.is_set() is woken, (keep_warm, woken)


def test_dispatched_work_refreshes_before_the_next_pong(monkeypatch: pytest.MonkeyPatch) -> None:
    # The runner reported False (its grace ran out) but its sandbox has not
    # expired yet; a message must renew it now, not at the next pong.
    submitted = _wire_recording_executor(monkeypatch)
    report = managed_host_keepalive.open_report("r1")
    managed_host_keepalive.record_pong("r1", False)
    assert managed_host_keepalive.touch("r1") is False
    report.wake.clear()
    managed_host_keepalive.record_work_dispatched("r1")
    assert report.wake.is_set(), "the loop is woken to refresh at once"
    assert managed_host_keepalive.touch("r1") is True
    assert submitted == ["r1"]
    # The runner's next pong reports the truth again.
    managed_host_keepalive.record_pong("r1", False)
    managed_host_keepalive._last_kept.clear()
    assert managed_host_keepalive.touch("r1") is False


def test_dispatched_work_leaves_a_refreshing_runner_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_recording_executor(monkeypatch)
    report = managed_host_keepalive.open_report("r1")
    for keep_warm in (True, None):
        managed_host_keepalive.record_pong("r1", keep_warm)
        report.wake.clear()
        managed_host_keepalive.record_work_dispatched("r1")
        assert not report.wake.is_set(), keep_warm
        assert report.keep_warm is keep_warm, "an older runner stays an older runner"


def test_a_reconnect_starts_from_its_own_first_pong(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire_recording_executor(monkeypatch)
    old = managed_host_keepalive.open_report("r1")
    managed_host_keepalive.record_pong("r1", True)
    new = managed_host_keepalive.open_report("r1")  # the runner reconnected
    managed_host_keepalive.close_report("r1", old)  # the old tunnel's teardown
    assert managed_host_keepalive._reports == {"r1": new}
    assert managed_host_keepalive.touch("r1") is False, "the new tunnel waits for its pong"
    managed_host_keepalive.close_report("r1", new)
    assert managed_host_keepalive._reports == {}


async def test_keepalive_loop_refreshes_on_reports_and_keeps_its_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server.routes import runner_tunnel
    from tests._helpers.polling import poll_until_async

    submitted = _wire_recording_executor(monkeypatch)
    submitted_at: list[float] = []
    record = managed_host_keepalive._executor.submit  # type: ignore[union-attr]
    loop = asyncio.get_running_loop()

    def _submit(*args: object) -> None:
        submitted_at.append(loop.time())
        record(*args)

    monkeypatch.setattr(managed_host_keepalive, "_executor", SimpleNamespace(submit=_submit))
    # Long enough that a refresh well inside it can only come from a wake.
    interval = 2.0
    monkeypatch.setattr(managed_host_keepalive, "_interval_for", lambda _rid: interval)

    task = asyncio.create_task(runner_tunnel._keepalive_loop("r1"))
    try:
        await asyncio.sleep(0.05)
        assert submitted == [], "the connect tick waits for the first pong"
        woken_at = loop.time()
        managed_host_keepalive.record_pong("r1", True)
        await poll_until_async(lambda: len(submitted) == 1, timeout_s=budget(1), interval_s=0.01)
        assert submitted_at[0] - woken_at < interval / 2, "woken at once, not at the next tick"
        # The wake restarted the schedule: the next refresh is a full interval on.
        await poll_until_async(lambda: len(submitted) == 2, timeout_s=budget(4), interval_s=0.01)
        assert submitted_at[1] - submitted_at[0] >= interval * 0.95
        managed_host_keepalive.record_pong("r1", False)
        await asyncio.sleep(interval * 1.2)
        assert len(submitted) == 2, "no refreshes while the runner says not to"
        resumed_at = loop.time()
        managed_host_keepalive.record_pong("r1", True)
        await poll_until_async(lambda: len(submitted) == 3, timeout_s=budget(1), interval_s=0.01)
        assert submitted_at[2] - resumed_at < interval / 2, "resumed work refreshes at once"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    assert managed_host_keepalive._reports == {}, "the loop releases its report"
