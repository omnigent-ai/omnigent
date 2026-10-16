"""Tests for the managed-path sandbox keepalive.

Covers the resolution chain (runner -> session -> host -> provider), the
per-runner rate limit, and the two skip paths (provider can't extend, host has
no sandbox). Stubs stand in for the stores/deployment: the module only reads a
few attributes off each, so a real store would add setup without adding cover.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from types import SimpleNamespace
from typing import cast

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


def _record_submission(submitted: list[str], *args: object) -> Future[None]:
    """Record a real executor submission and return its concrete Future type."""
    submitted.append(cast(str, args[-2]))
    return Future()


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
        host=SimpleNamespace(sandbox_id="sbx1", sandbox_provider="modal"),
    )
    managed_host_keepalive._keep_alive_for_runner("r1")
    assert launcher.calls == ["sbx1"]


def test_provider_without_keep_alive_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    # kubernetes today: the base class raises, and that must not propagate.
    launcher = _Launcher(raises=SandboxCapabilityError("nope"))
    _wire(
        monkeypatch,
        launcher=launcher,
        host=SimpleNamespace(sandbox_id="sbx1", sandbox_provider="kubernetes"),
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
    # touch() submits a worker start timestamp after the runner id.
    monkeypatch.setattr(
        managed_host_keepalive,
        "_executor",
        SimpleNamespace(submit=lambda *args: _record_submission(submitted, *args)),
    )
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    monkeypatch.setattr(managed_host_keepalive, "_retry_after", {})

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
        SimpleNamespace(submit=lambda *args: Future()),
    )
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    monkeypatch.setattr(managed_host_keepalive, "_retry_after", {})
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
    monkeypatch.setattr(managed_host_keepalive, "_retry_after", {})
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
        get_host=lambda _hid: SimpleNamespace(sandbox_id="sbx1", sandbox_provider="modal")
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
        SimpleNamespace(submit=lambda *args: _record_submission(submitted, *args)),
    )
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    monkeypatch.setattr(managed_host_keepalive, "_retry_after", {})

    managed_host_keepalive.touch("r1")
    # Past the throttle window, but the first attempt has not finished.
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    managed_host_keepalive.touch("r1")
    assert submitted == ["r1"]

    # Once it clears, the next tick submits again.
    managed_host_keepalive._inflight.discard("r1")
    managed_host_keepalive.touch("r1")
    assert submitted == ["r1", "r1"]


def test_helper_cannot_release_a_new_reservation_at_worker_handoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The worker owns release after the helper returns.

    The old two-finalizer sequence released ``r1`` inside the helper, then
    allowed a second touch to reserve it before the worker wrapper's finalizer
    ran. This test pauses at exactly that boundary and proves the reservation
    remains held until the wrapper is finished.
    """
    helper_returned = threading.Event()
    allow_worker_return = threading.Event()
    helper_calls = 0
    launcher = _Launcher()
    _wire(
        monkeypatch,
        launcher=launcher,
        host=SimpleNamespace(sandbox_id="sbx1", sandbox_provider="modal"),
    )
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="test-managed-keepalive")
    monkeypatch.setattr(managed_host_keepalive, "_executor", pool)
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_runner_interval_s", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    monkeypatch.setattr(managed_host_keepalive, "_retry_after", {})
    original_helper = managed_host_keepalive._keep_alive_for_runner

    def _pause_after_helper(runner_id: str) -> None:
        nonlocal helper_calls
        helper_calls += 1
        original_helper(runner_id)
        helper_returned.set()
        assert allow_worker_return.wait(timeout=2.0)

    monkeypatch.setattr(managed_host_keepalive, "_keep_alive_for_runner", _pause_after_helper)
    try:
        managed_host_keepalive.touch("r1")
        assert helper_returned.wait(timeout=1.0)
        with managed_host_keepalive._state_lock:
            assert "r1" in managed_host_keepalive._inflight
            managed_host_keepalive._last_kept.clear()

        # A second tick is suppressed while the first worker still owns the
        # reservation. The old helper-side release made this submit a duplicate.
        managed_host_keepalive.touch("r1")
        assert helper_calls == 1
    finally:
        allow_worker_return.set()
        pool.shutdown(wait=True)


def test_stalled_runner_does_not_block_an_independent_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A provider stall for one runner must not starve another runner."""
    started: dict[str, threading.Event] = {
        "slow": threading.Event(),
        "healthy": threading.Event(),
    }
    release_slow = threading.Event()

    def _keep_alive(runner_id: str) -> None:
        started[runner_id].set()
        if runner_id == "slow":
            assert release_slow.wait(timeout=2.0)

    pool = ThreadPoolExecutor(
        max_workers=managed_host_keepalive._KEEPALIVE_MAX_WORKERS,
        thread_name_prefix="test-managed-keepalive",
    )
    monkeypatch.setattr(managed_host_keepalive, "_executor", pool)
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", object())
    monkeypatch.setattr(managed_host_keepalive, "_host_store", object())
    monkeypatch.setattr(managed_host_keepalive, "_keep_alive_for_runner", _keep_alive)
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_runner_interval_s", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    monkeypatch.setattr(managed_host_keepalive, "_retry_after", {})
    try:
        managed_host_keepalive.touch("slow")
        assert started["slow"].wait(timeout=1.0)
        managed_host_keepalive.touch("healthy")
        assert started["healthy"].wait(timeout=1.0), (
            "a stalled provider occupied all keepalive capacity"
        )
    finally:
        release_slow.set()
        pool.shutdown(wait=True)


def test_worker_pool_has_a_fixed_bound() -> None:
    """Keepalive concurrency stays bounded independently of runner count."""
    assert managed_host_keepalive._KEEPALIVE_MAX_WORKERS == 8


def test_saturated_pool_keeps_a_queued_runner_single_flight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued runner resumes once capacity returns without a submission burst."""
    busy_runner_ids = [
        f"busy-{index}" for index in range(managed_host_keepalive._KEEPALIVE_MAX_WORKERS)
    ]
    busy_started = {runner_id: threading.Event() for runner_id in busy_runner_ids}
    release_busy = threading.Event()
    queued_started = threading.Event()
    submitted: list[str] = []
    helper_calls: list[str] = []
    calls_lock = threading.Lock()

    def _keep_alive(runner_id: str) -> None:
        with calls_lock:
            helper_calls.append(runner_id)
        if runner_id in busy_started:
            busy_started[runner_id].set()
            assert release_busy.wait(timeout=2.0)
        else:
            queued_started.set()

    pool = ThreadPoolExecutor(
        max_workers=managed_host_keepalive._KEEPALIVE_MAX_WORKERS,
        thread_name_prefix="test-managed-keepalive",
    )

    class _CountingExecutor:
        def submit(self, *args: object) -> Future[None]:
            submitted.append(cast(str, args[-2]))
            return pool.submit(*args)

    monkeypatch.setattr(managed_host_keepalive, "_executor", _CountingExecutor())
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", object())
    monkeypatch.setattr(managed_host_keepalive, "_host_store", object())
    monkeypatch.setattr(managed_host_keepalive, "_keep_alive_for_runner", _keep_alive)
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_runner_interval_s", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    monkeypatch.setattr(managed_host_keepalive, "_retry_after", {})
    try:
        for runner_id in busy_runner_ids:
            managed_host_keepalive.touch(runner_id)
        assert all(event.wait(timeout=1.0) for event in busy_started.values())

        managed_host_keepalive.touch("queued")
        for _ in range(5):
            managed_host_keepalive.touch("queued")
        assert submitted.count("queued") == 1
        with managed_host_keepalive._state_lock:
            assert "queued" in managed_host_keepalive._inflight
            assert "queued" not in managed_host_keepalive._last_kept

        release_busy.set()
        assert queued_started.wait(timeout=1.0)
        deadline = time.monotonic() + 1.0
        while True:
            with managed_host_keepalive._state_lock:
                queued_finished = "queued" not in managed_host_keepalive._inflight
            if queued_finished or time.monotonic() >= deadline:
                break
            time.sleep(0.001)
        assert queued_finished

        for _ in range(5):
            managed_host_keepalive.touch("queued")
        with calls_lock:
            assert helper_calls.count("queued") == 1
        assert submitted.count("queued") == 1
    finally:
        release_busy.set()
        pool.shutdown(wait=True)


def test_throttle_is_stamped_when_a_queued_worker_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A queued job is not treated as refreshed until its provider work starts."""
    slow_started = threading.Event()
    release_slow = threading.Event()
    healthy_started = threading.Event()

    def _keep_alive(runner_id: str) -> None:
        if runner_id == "slow":
            slow_started.set()
            assert release_slow.wait(timeout=2.0)
        else:
            healthy_started.set()

    pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="test-managed-keepalive")
    monkeypatch.setattr(managed_host_keepalive, "_executor", pool)
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", object())
    monkeypatch.setattr(managed_host_keepalive, "_host_store", object())
    monkeypatch.setattr(managed_host_keepalive, "_keep_alive_for_runner", _keep_alive)
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_runner_interval_s", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    monkeypatch.setattr(managed_host_keepalive, "_retry_after", {})
    try:
        managed_host_keepalive.touch("slow")
        assert slow_started.wait(timeout=1.0)
        queued_at = time.monotonic()
        managed_host_keepalive.touch("healthy")
        assert not healthy_started.is_set()
        assert "healthy" not in managed_host_keepalive._last_kept

        release_slow.set()
        assert healthy_started.wait(timeout=1.0)
        started_at = managed_host_keepalive._last_kept["healthy"]
        assert started_at >= queued_at
        assert started_at - queued_at < 1.0
    finally:
        release_slow.set()
        pool.shutdown(wait=True)


def test_next_delay_retries_an_early_tick_after_worker_start_lag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A due tick cannot defer the next refresh by another full interval."""
    monkeypatch.setattr(managed_host_keepalive, "_runner_interval_s", {"r1": 60.0})
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {"r1": 0.005})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    monkeypatch.setattr(managed_host_keepalive, "_retry_after", {})
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", object())
    monkeypatch.setattr(managed_host_keepalive, "_host_store", object())
    monkeypatch.setattr(managed_host_keepalive, "_executor", object())

    # The loop wakes at 60.001s after a worker started at 0.005s: the
    # remaining cadence is already due, so it must touch immediately.
    assert managed_host_keepalive.next_keepalive_delay_s("r1", now=60.001) == pytest.approx(0.004)

    # If the provider is still running at the due point, retry shortly rather
    # than sleeping another 60s and crossing a 2x-interval shutdown window.
    monkeypatch.setattr(managed_host_keepalive, "_inflight", {"r1"})
    assert managed_host_keepalive.next_keepalive_delay_s("r1", now=60.005) == 1.0

    # A rejected submission can leave no worker in flight; overdue retries must
    # still be paced instead of returning zero and spinning the loop.
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    assert managed_host_keepalive.next_keepalive_delay_s("r1", now=60.005) == 1.0


def test_submission_failure_releases_the_runner_reservation(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A failed submit must not permanently suppress later refresh attempts."""
    submitted: list[object] = []

    class _RejectingExecutor:
        def submit(self, *_args: object) -> None:
            raise RuntimeError("test submission failure")

    monkeypatch.setattr(managed_host_keepalive, "_executor", _RejectingExecutor())
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", object())
    monkeypatch.setattr(managed_host_keepalive, "_host_store", object())
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_runner_interval_s", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    monkeypatch.setattr(managed_host_keepalive, "_retry_after", {})

    with caplog.at_level(logging.WARNING, logger="omnigent.server.managed_host_keepalive"):
        managed_host_keepalive.touch("r1")
    assert "r1" not in managed_host_keepalive._inflight
    assert "r1" not in managed_host_keepalive._last_kept
    assert any(
        getattr(record, "attributes", {}).get("outcome") == "submission_failed"
        for record in caplog.records
    )
    submission_event = next(
        record
        for record in caplog.records
        if getattr(record, "attributes", {}).get("outcome") == "submission_failed"
    )
    assert submission_event.attributes["error_type"] == "RuntimeError"
    retry_at = managed_host_keepalive._retry_after["r1"]
    assert retry_at > time.monotonic()

    monkeypatch.setattr(
        managed_host_keepalive,
        "_executor",
        SimpleNamespace(submit=lambda *args: _record_submission(submitted, *args)),
    )
    monkeypatch.setattr(managed_host_keepalive.time, "monotonic", lambda: retry_at)
    managed_host_keepalive.touch("r1")
    assert submitted == ["r1"]
    assert managed_host_keepalive._retry_after == {}


def test_rejected_submissions_follow_a_bounded_retry_cadence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejecting executor cannot turn the tunnel loop into a tight spin."""
    clock = 100.0
    attempts = 0

    class _RejectingExecutor:
        def submit(self, *_args: object) -> None:
            nonlocal attempts
            attempts += 1
            raise RuntimeError("test submission failure")

    monkeypatch.setattr(managed_host_keepalive.time, "monotonic", lambda: clock)
    monkeypatch.setattr(managed_host_keepalive, "_executor", _RejectingExecutor())
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", object())
    monkeypatch.setattr(managed_host_keepalive, "_host_store", object())
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_runner_interval_s", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    monkeypatch.setattr(managed_host_keepalive, "_retry_after", {})

    for expected_attempts in range(1, 4):
        managed_host_keepalive.touch("r1")
        assert attempts == expected_attempts
        retry_at = managed_host_keepalive._retry_after["r1"]
        assert managed_host_keepalive.next_keepalive_delay_s(
            "r1", now=retry_at - 0.25
        ) == pytest.approx(0.25)

        clock = retry_at - 0.01
        managed_host_keepalive.touch("r1")
        assert attempts == expected_attempts
        assert managed_host_keepalive._inflight == set()
        clock = retry_at


def test_failed_only_runners_prune_expired_retry_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rejected submissions do not grow retry state without worker starts."""
    clock = 100.0

    class _RejectingExecutor:
        def submit(self, *_args: object) -> None:
            raise RuntimeError("test submission failure")

    monkeypatch.setattr(managed_host_keepalive.time, "monotonic", lambda: clock)
    monkeypatch.setattr(managed_host_keepalive, "_THROTTLE_MAX_ENTRIES", 2)
    monkeypatch.setattr(managed_host_keepalive, "_executor", _RejectingExecutor())
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", object())
    monkeypatch.setattr(managed_host_keepalive, "_host_store", object())
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_runner_interval_s", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    monkeypatch.setattr(managed_host_keepalive, "_retry_after", {})

    for index in range(7):
        managed_host_keepalive.touch(f"rejected-{index}")
        assert managed_host_keepalive._last_kept == {}
        clock += 2.0

    assert managed_host_keepalive._retry_after == {"rejected-6": pytest.approx(113.0)}


def test_next_delay_uses_the_interval_when_managed_keepalive_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stale throttle state cannot make a disabled keepalive loop spin."""
    monkeypatch.setattr(managed_host_keepalive, "_runner_interval_s", {"r1": 60.0})
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {"r1": 0.005})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    monkeypatch.setattr(managed_host_keepalive, "_retry_after", {})
    monkeypatch.setattr(managed_host_keepalive, "_sandbox_config", None)
    monkeypatch.setattr(managed_host_keepalive, "_host_store", None)
    monkeypatch.setattr(managed_host_keepalive, "_executor", None)

    assert managed_host_keepalive.next_keepalive_delay_s("r1", now=60.005) == 60.0


def test_worker_releases_reservation_when_the_provider_raises(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The scheduling wrapper releases a failed provider reservation."""
    launcher = _Launcher(raises=RuntimeError("boom"))
    _wire(
        monkeypatch,
        launcher=launcher,
        host=SimpleNamespace(sandbox_id="sbx1", sandbox_provider="modal"),
    )
    monkeypatch.setattr(managed_host_keepalive, "_inflight", {"r1"})
    with caplog.at_level(logging.WARNING, logger="omnigent.server.managed_host_keepalive"):
        managed_host_keepalive._run_keepalive_job("r1", time.monotonic())
    assert "r1" not in managed_host_keepalive._inflight
    error_events = [
        record
        for record in caplog.records
        if getattr(record, "attributes", {}).get("outcome") == "provider_error"
    ]
    assert error_events
    assert all("boom" not in record.getMessage() for record in error_events)
    assert error_events[0].attributes["error_type"] == "RuntimeError"


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


def test_worker_evidence_contains_queue_and_provider_duration(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Worker events carry bounded scheduling and provider timing evidence."""

    class _SignalingLauncher(_Launcher):
        def __init__(self) -> None:
            super().__init__()
            self.started = threading.Event()

        def keep_alive(self, sandbox_id: str) -> object:
            self.started.set()
            return super().keep_alive(sandbox_id)

    launcher = _SignalingLauncher()
    _wire(
        monkeypatch,
        launcher=launcher,
        host=SimpleNamespace(sandbox_id="sbx1", sandbox_provider="agent_sandbox"),
    )
    pool = ThreadPoolExecutor(
        max_workers=managed_host_keepalive._KEEPALIVE_MAX_WORKERS,
        thread_name_prefix="test-managed-keepalive",
    )
    monkeypatch.setattr(managed_host_keepalive, "_executor", pool)
    monkeypatch.setattr(managed_host_keepalive, "_last_kept", {})
    monkeypatch.setattr(managed_host_keepalive, "_runner_interval_s", {})
    monkeypatch.setattr(managed_host_keepalive, "_inflight", set())
    monkeypatch.setattr(managed_host_keepalive, "_retry_after", {})
    try:
        with caplog.at_level(logging.INFO, logger="omnigent.server.managed_host_keepalive"):
            managed_host_keepalive.touch("r1")
            assert launcher.started.wait(timeout=1.0)
            pool.shutdown(wait=True)
        event = next(
            r for r in caplog.records if getattr(r, "attributes", {}).get("outcome") == "extended"
        )
        assert event.attributes["queue_delay_s"] >= 0
        assert event.attributes["provider_duration_s"] >= 0
    finally:
        pool.shutdown(wait=True)


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
