"""Tests for the managed-path sandbox keepalive.

Covers the resolution chain (runner -> session -> host -> provider), the
per-runner rate limit, and the two skip paths (provider can't extend, host has
no sandbox). Stubs stand in for the stores/deployment: the module only reads a
few attributes off each, so a real store would add setup without adding cover.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.onboarding.sandboxes.base import SandboxCapabilityError
from omnigent.server import managed_host_keepalive


class _Launcher:
    def __init__(self, raises: BaseException | None = None) -> None:
        self.calls: list[str] = []
        self._raises = raises

    def keep_alive(self, sandbox_id: str) -> None:
        self.calls.append(sandbox_id)
        if self._raises is not None:
            raise self._raises


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
        host=SimpleNamespace(sandbox_id="sbx1", sandbox_provider="modal"),
    )
    monkeypatch.setattr(managed_host_keepalive, "_inflight", {"r1"})
    managed_host_keepalive._keep_alive_for_runner("r1")
    assert "r1" not in managed_host_keepalive._inflight
