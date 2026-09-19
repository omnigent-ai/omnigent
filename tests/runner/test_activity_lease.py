"""Provider-neutral runner activity lease tests."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace

import pytest

from omnigent.runner import activity_lease as activity_lease_module
from omnigent.runner.activity_lease import (
    _ACTIVITY_LEASE_FACTORIES,
    _BUILTIN_LEASE_MODULES,
    ACTIVITY_LEASE_PROVIDER_ENV_VAR,
    ActivityLeaseError,
    activity_lease_from_env,
    register_activity_lease_provider,
    run_activity_lease,
)

pytestmark = pytest.mark.asyncio


class RecordingLease:
    """Controllable lease adapter for state-machine tests."""

    provider = "test"

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail_release = False

    async def refresh(self) -> None:
        self.calls.append("refresh")

    async def release(self) -> None:
        self.calls.append("release")
        if self.fail_release:
            self.fail_release = False
            raise ActivityLeaseError("transient release failure")

    async def aclose(self) -> None:
        self.calls.append("close")


async def _wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("timed out waiting for activity lease operation")


async def test_backend_requires_explicit_provider_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Filesystem artifacts alone never activate a provider lease."""
    monkeypatch.delenv(ACTIVITY_LEASE_PROVIDER_ENV_VAR, raising=False)
    assert activity_lease_from_env("runner-123") is None


async def test_registered_backend_is_selected_explicitly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The configured provider selects its registered adapter factory."""
    lease = RecordingLease()

    def factory(runner_id: str) -> RecordingLease:
        assert runner_id == "runner-123"
        return lease

    register_activity_lease_provider("test", factory)
    monkeypatch.setenv(ACTIVITY_LEASE_PROVIDER_ENV_VAR, "test")
    try:
        assert activity_lease_from_env("runner-123") is lease
    finally:
        _ACTIVITY_LEASE_FACTORIES.pop("test", None)


async def test_configured_builtin_module_is_loaded_lazily(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only the provider named by the environment imports its adapter module."""
    lease = RecordingLease()
    imported: list[str] = []

    def register() -> None:
        register_activity_lease_provider("lazy", lambda runner_id: lease)

    def import_module(module_name: str) -> object:
        imported.append(module_name)
        return SimpleNamespace(register_activity_lease_provider=register)

    monkeypatch.setitem(_BUILTIN_LEASE_MODULES, "lazy", "example.lazy_lease")
    monkeypatch.setattr(activity_lease_module.importlib, "import_module", import_module)
    monkeypatch.setenv(ACTIVITY_LEASE_PROVIDER_ENV_VAR, "lazy")
    try:
        assert activity_lease_from_env("runner-123") is lease
        assert imported == ["example.lazy_lease"]
    finally:
        _ACTIVITY_LEASE_FACTORIES.pop("lazy", None)


@pytest.mark.parametrize("provider", ["", "unknown"])
async def test_invalid_backend_configuration_fails_loud(
    monkeypatch: pytest.MonkeyPatch,
    provider: str,
) -> None:
    """A typo cannot silently disable hibernation protection."""
    monkeypatch.setenv(ACTIVITY_LEASE_PROVIDER_ENV_VAR, provider)
    with pytest.raises(RuntimeError, match=r"activity lease provider|must not be empty"):
        activity_lease_from_env("runner-123")


async def test_active_work_refreshes_then_releases_lease() -> None:
    """Work holds the provider lease and draining releases it."""
    active = True
    lease = RecordingLease()
    task = asyncio.create_task(
        run_activity_lease(
            lease=lease,
            has_active_work=lambda: active,
            poll_interval_s=0.005,
            refresh_interval_s=60,
            release_grace_s=0,
        )
    )
    await _wait_until(lambda: "refresh" in lease.calls)
    active = False
    await _wait_until(lambda: "release" in lease.calls)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert lease.calls == ["refresh", "release", "close"]


async def test_idle_grace_survives_a_transient_status_gap() -> None:
    """A brief gap between turn and terminal work does not drop the hold."""
    active = True
    lease = RecordingLease()
    task = asyncio.create_task(
        run_activity_lease(
            lease=lease,
            has_active_work=lambda: active,
            poll_interval_s=0.005,
            refresh_interval_s=60,
            release_grace_s=0.05,
        )
    )
    await _wait_until(lambda: "refresh" in lease.calls)
    active = False
    await asyncio.sleep(0.02)
    active = True
    await asyncio.sleep(0.02)
    assert "release" not in lease.calls

    active = False
    await _wait_until(lambda: "release" in lease.calls)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert lease.calls == ["refresh", "release", "close"]


async def test_release_failure_retries_without_losing_held_state() -> None:
    """A transient provider failure retries release until it succeeds."""
    active = True
    lease = RecordingLease()
    lease.fail_release = True
    task = asyncio.create_task(
        run_activity_lease(
            lease=lease,
            has_active_work=lambda: active,
            poll_interval_s=0.005,
            refresh_interval_s=60,
            release_grace_s=0,
            retry_interval_s=0.01,
        )
    )
    await _wait_until(lambda: "refresh" in lease.calls)
    active = False
    await _wait_until(lambda: lease.calls.count("release") == 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert lease.calls == ["refresh", "release", "release", "close"]


async def test_cancellation_releases_a_live_lease() -> None:
    """Orderly runner shutdown drops the provider hold immediately."""
    lease = RecordingLease()
    task = asyncio.create_task(
        run_activity_lease(
            lease=lease,
            has_active_work=lambda: True,
            poll_interval_s=0.005,
        )
    )
    await _wait_until(lambda: "refresh" in lease.calls)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert lease.calls == ["refresh", "release", "close"]
