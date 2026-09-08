"""Tests for :mod:`omnigent.server.managed_sandbox_reaper`."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator
from dataclasses import replace

import pytest

from omnigent.db.db_models import current_workspace_id
from omnigent.server.managed_hosts import (
    ManagedSandboxConfig,
    ManagedSandboxDeployment,
    ManagedSandboxReaperConfig,
)
from omnigent.server.managed_sandbox_reaper import ManagedSandboxReaper
from omnigent.stores.host_store import Host

pytestmark = pytest.mark.asyncio

_DAY_S = 24 * 60 * 60


class _RecordingLauncher:
    def __init__(
        self,
        provider: str,
        *,
        fail: bool = False,
        identity_fail: bool = False,
        on_terminate: Callable[[str], None] | None = None,
    ) -> None:
        self.provider = provider
        self.fail = fail
        self.identity_fail = identity_fail
        self.on_terminate = on_terminate
        self.factory_calls = 0
        self.identity_entries: list[int] = []
        self.identity_threads: list[int] = []
        self.terminated: list[str] = []
        self.termination_threads: list[int] = []

    @contextlib.contextmanager
    def reaper_identity(self, workspace_id: int) -> Iterator[None]:
        self.identity_entries.append(workspace_id)
        self.identity_threads.append(threading.get_ident())
        if self.identity_fail:
            raise RuntimeError(f"{self.provider} identity unavailable")
        yield

    def terminate(self, sandbox_id: str) -> None:
        self.termination_threads.append(threading.get_ident())
        if self.on_terminate is not None:
            self.on_terminate(sandbox_id)
        if self.fail:
            raise RuntimeError(f"{self.provider} unavailable")
        self.terminated.append(sandbox_id)


class _FakeHostStore:
    def __init__(self, hosts: dict[int, list[Host]]) -> None:
        self.hosts = {
            workspace_id: {host.host_id: host for host in workspace_hosts}
            for workspace_id, workspace_hosts in hosts.items()
        }
        self.detached: list[tuple[int, str, str]] = []
        self.cleared: list[tuple[int, str, str]] = []
        self.refresh_on_detach: set[str] = set()
        self.stale_queries: list[tuple[int, int]] = []

    def list_managed_sandbox_workspace_ids(self) -> list[int]:
        return sorted(
            workspace_id
            for workspace_id, hosts in self.hosts.items()
            if any(
                host.sandbox_provider is not None
                and (host.sandbox_id is not None or host.terminating_sandbox_id is not None)
                for host in hosts.values()
            )
        )

    def list_stale_managed_sandbox_hosts(self, older_than_epoch: int) -> list[Host]:
        workspace_id = current_workspace_id()
        self.stale_queries.append((workspace_id, older_than_epoch))
        hosts = [
            host
            for host in self.hosts[workspace_id].values()
            if host.sandbox_provider is not None
            and (
                host.terminating_sandbox_id is not None
                or (host.sandbox_id is not None and host.updated_at <= older_than_epoch)
            )
        ]
        return sorted(hosts, key=lambda host: (host.updated_at, host.host_id))

    def detach_stale_managed_sandbox(
        self,
        host_id: str,
        *,
        sandbox_id: str,
        expected_updated_at: int,
    ) -> bool:
        workspace_id = current_workspace_id()
        workspace_hosts = self.hosts[current_workspace_id()]
        host = workspace_hosts.get(host_id)
        if host is not None and host_id in self.refresh_on_detach:
            host = replace(host, status="online", updated_at=4_000_000)
            workspace_hosts[host_id] = host
            self.refresh_on_detach.remove(host_id)
        if (
            host is None
            or host.sandbox_id != sandbox_id
            or host.updated_at != expected_updated_at
            or host.terminating_sandbox_id is not None
        ):
            return False
        workspace_hosts[host_id] = replace(
            host,
            status="offline",
            sandbox_id=None,
            terminating_sandbox_id=sandbox_id,
        )
        self.detached.append((workspace_id, host_id, sandbox_id))
        return True

    def mark_terminating_sandbox_terminated(
        self,
        host_id: str,
        *,
        sandbox_id: str,
    ) -> bool:
        workspace_id = current_workspace_id()
        host = self.hosts[workspace_id].get(host_id)
        if host is None or host.terminating_sandbox_id != sandbox_id:
            return False
        self.hosts[workspace_id][host_id] = replace(
            host,
            terminating_sandbox_id=None,
        )
        self.cleared.append((workspace_id, host_id, sandbox_id))
        return True


def _host(
    host_id: str,
    *,
    provider: str,
    sandbox_id: str | None,
    updated_at: int,
    status: str = "offline",
    terminating_sandbox_id: str | None = None,
) -> Host:
    return Host(
        host_id=host_id,
        name=host_id,
        user_id="alice@example.com",
        status=status,
        created_at=updated_at - 100,
        updated_at=updated_at,
        sandbox_provider=provider,
        sandbox_id=sandbox_id,
        terminating_sandbox_id=terminating_sandbox_id,
    )


def _deployment(
    launchers: list[_RecordingLauncher],
    *,
    terminate_after_offline_days: int = 1,
    sweep_interval_s: int = 86400,
) -> ManagedSandboxDeployment:
    configs = []
    for launcher in launchers:

        def launcher_factory(launcher: _RecordingLauncher = launcher) -> _RecordingLauncher:
            launcher.factory_calls += 1
            return launcher

        configs.append(
            ManagedSandboxConfig(
                server_url="https://s.example.com",
                launcher_factory=launcher_factory,
                token_ttl_s=3600,
                provider=launcher.provider,
            )
        )
    return ManagedSandboxDeployment(
        configs=tuple(configs),
        reaper=ManagedSandboxReaperConfig(
            enabled=True,
            terminate_after_offline_days=terminate_after_offline_days,
            sweep_interval_s=sweep_interval_s,
        ),
    )


async def test_sweep_reaps_stale_host_rows_without_scanning_sessions() -> None:
    now = 4_000_000
    modal = _RecordingLauncher("modal")
    e2b = _RecordingLauncher("e2b")
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_recent",
                    provider="modal",
                    sandbox_id="sb-recent",
                    updated_at=now - _DAY_S + 1,
                ),
                _host(
                    "host_modal",
                    provider="modal",
                    sandbox_id="sb-modal",
                    updated_at=now - _DAY_S - 1,
                ),
                _host(
                    "host_modal_2",
                    provider="modal",
                    sandbox_id="sb-modal-2",
                    updated_at=now - 2 * _DAY_S,
                ),
            ],
            9: [
                _host(
                    "host_e2b",
                    provider="e2b",
                    sandbox_id="sb-e2b",
                    updated_at=now - 2 * _DAY_S,
                    status="online",
                )
            ],
        }
    )
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([modal, e2b]),
    )

    assert await reaper.sweep_once(now=now) == 3

    assert modal.terminated == ["sb-modal-2", "sb-modal"]
    assert e2b.terminated == ["sb-e2b"]
    assert hosts.detached == [
        (7, "host_modal_2", "sb-modal-2"),
        (7, "host_modal", "sb-modal"),
        (9, "host_e2b", "sb-e2b"),
    ]
    assert hosts.cleared == hosts.detached
    assert modal.factory_calls == 1
    assert modal.identity_entries == [7]
    assert e2b.factory_calls == 1
    assert e2b.identity_entries == [9]
    assert hosts.hosts[7]["host_modal"].sandbox_id is None
    assert hosts.hosts[7]["host_modal"].terminating_sandbox_id is None
    assert hosts.hosts[7]["host_recent"].sandbox_id == "sb-recent"
    assert hosts.stale_queries == [(7, now - _DAY_S), (9, now - _DAY_S)]


async def test_provider_failure_does_not_stop_other_sandboxes() -> None:
    now = 4_000_000
    modal = _RecordingLauncher("modal", fail=True)
    e2b = _RecordingLauncher("e2b")
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_modal",
                    provider="modal",
                    sandbox_id="sb-modal",
                    updated_at=now - 2 * _DAY_S,
                ),
                _host(
                    "host_e2b",
                    provider="e2b",
                    sandbox_id="sb-e2b",
                    updated_at=now - 2 * _DAY_S,
                ),
            ]
        }
    )
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([modal, e2b]),
    )

    assert await reaper.sweep_once(now=now) == 1
    assert hosts.hosts[7]["host_modal"].sandbox_id is None
    assert hosts.hosts[7]["host_modal"].terminating_sandbox_id == "sb-modal"
    assert hosts.hosts[7]["host_e2b"].sandbox_id is None
    assert e2b.terminated == ["sb-e2b"]


async def test_provider_identity_failure_does_not_stop_other_sandboxes() -> None:
    now = 4_000_000
    modal = _RecordingLauncher("modal", identity_fail=True)
    e2b = _RecordingLauncher("e2b")
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_modal",
                    provider="modal",
                    sandbox_id="sb-modal",
                    updated_at=now - 2 * _DAY_S,
                ),
                _host(
                    "host_e2b",
                    provider="e2b",
                    sandbox_id="sb-e2b",
                    updated_at=now - 2 * _DAY_S,
                ),
            ]
        }
    )
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([modal, e2b]),
    )

    assert await reaper.sweep_once(now=now) == 1
    assert modal.terminated == []
    assert e2b.terminated == ["sb-e2b"]
    assert hosts.hosts[7]["host_modal"].sandbox_id == "sb-modal"
    assert hosts.hosts[7]["host_e2b"].sandbox_id is None


async def test_reaper_rereads_liveness_before_termination() -> None:
    now = 4_000_000
    modal = _RecordingLauncher("modal")
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_modal",
                    provider="modal",
                    sandbox_id="sb-modal",
                    updated_at=now - 2 * _DAY_S,
                )
            ]
        }
    )
    hosts.refresh_on_detach.add("host_modal")
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([modal]),
    )

    assert await reaper.sweep_once(now=now) == 0
    assert modal.terminated == []
    assert hosts.hosts[7]["host_modal"].sandbox_id == "sb-modal"


async def test_successful_termination_preserves_new_active_generation() -> None:
    now = 4_000_000
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_modal",
                    provider="modal",
                    sandbox_id="sb-modal",
                    updated_at=now - 2 * _DAY_S,
                )
            ]
        }
    )

    def _launch_new_generation(sandbox_id: str) -> None:
        assert sandbox_id == "sb-modal"
        current = hosts.hosts[7]["host_modal"]
        assert current.sandbox_id is None
        assert current.terminating_sandbox_id == "sb-modal"
        hosts.hosts[7]["host_modal"] = replace(
            current,
            sandbox_id="sb-modal-new",
            status="online",
            updated_at=now,
        )

    modal = _RecordingLauncher("modal", on_terminate=_launch_new_generation)
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([modal]),
    )

    assert await reaper.sweep_once(now=now) == 1
    assert modal.terminated == ["sb-modal"]
    assert hosts.hosts[7]["host_modal"].sandbox_id == "sb-modal-new"
    assert hosts.hosts[7]["host_modal"].terminating_sandbox_id is None


async def test_pending_termination_retries_without_detaching_active_generation() -> None:
    now = 4_000_000
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_modal",
                    provider="modal",
                    sandbox_id="sb-modal-new",
                    terminating_sandbox_id="sb-modal-old",
                    updated_at=now,
                    status="online",
                )
            ]
        }
    )
    modal = _RecordingLauncher("modal")
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([modal]),
    )

    assert await reaper.sweep_once(now=now) == 1
    assert modal.terminated == ["sb-modal-old"]
    assert hosts.detached == []
    assert hosts.hosts[7]["host_modal"].sandbox_id == "sb-modal-new"
    assert hosts.hosts[7]["host_modal"].terminating_sandbox_id is None


async def test_pending_id_equal_to_active_id_is_not_terminated() -> None:
    now = 4_000_000
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_blaxel",
                    provider="blaxel",
                    sandbox_id="managed-host_blaxel",
                    terminating_sandbox_id="managed-host_blaxel",
                    updated_at=now,
                    status="online",
                )
            ]
        }
    )
    blaxel = _RecordingLauncher("blaxel")
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([blaxel]),
    )

    assert await reaper.sweep_once(now=now) == 0
    assert blaxel.terminated == []
    current = hosts.hosts[7]["host_blaxel"]
    assert current.sandbox_id == "managed-host_blaxel"
    assert current.terminating_sandbox_id == "managed-host_blaxel"


async def test_identity_and_termination_run_off_event_loop_thread() -> None:
    now = 4_000_000
    event_loop_thread = threading.get_ident()
    modal = _RecordingLauncher("modal")
    hosts = _FakeHostStore(
        {
            7: [
                _host(
                    "host_modal",
                    provider="modal",
                    sandbox_id="sb-modal",
                    updated_at=now - 2 * _DAY_S,
                )
            ]
        }
    )
    reaper = ManagedSandboxReaper(
        host_store=hosts,  # type: ignore[arg-type]
        sandbox_config=_deployment([modal]),
    )

    assert await reaper.sweep_once(now=now) == 1
    assert modal.identity_threads == modal.termination_threads
    assert modal.identity_threads[0] != event_loop_thread
