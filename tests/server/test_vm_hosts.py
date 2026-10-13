"""The static sandbox host supervisor, against the fake shim."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from omnigent.community.sandbox.remote import launcher as remote_launcher
from omnigent.db.utils import now_epoch
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.onboarding.sandboxes.base import SandboxHostLauncher
from omnigent.server import vm_hosts
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from omnigent.stores.host_store import HostStore
from tests.server.static_sandbox_helpers import OWNER, WS, VmShim, make_deployment, make_shim

pytestmark = pytest.mark.asyncio


@pytest.fixture()
def host_store(db_uri: str) -> HostStore:
    return HostStore(db_uri)


@pytest.fixture()
def shim(host_store: HostStore) -> VmShim:
    return make_shim(host_store)


@pytest.fixture()
def deployment(shim: VmShim) -> ManagedSandboxDeployment:
    return make_deployment(shim)


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    vm_hosts._lookup_cache.clear()
    vm_hosts._providers_cache.clear()
    vm_hosts._recent_failures.clear()


@pytest.fixture()
def fast_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """Shrink the host-online wait; the failure probe runs on every poll."""
    from omnigent.server import managed_hosts

    monkeypatch.setattr(managed_hosts, "MANAGED_HOST_ONLINE_TIMEOUT_S", 0.3)
    monkeypatch.setattr(managed_hosts, "_ONLINE_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr(managed_hosts, "_FAILURE_PROBE_INTERVAL_S", 0.0)


class _PlainLauncher(SandboxHostLauncher):
    """A provider with no static-sandbox operations."""

    provider = "plain"

    def prepare(self) -> None: ...

    def provision(self, name: str) -> str:
        return name

    def terminate(self, sandbox_id: str) -> None: ...

    def start_host(self, sandbox_id: str, **kwargs: object) -> str:
        return "/workspace"


async def _ensure(deployment: ManagedSandboxDeployment, host_store: HostStore, **kw: Any) -> str:
    return await vm_hosts.ensure_vm_host(
        owner=OWNER,
        provider="remote",
        sandbox_id=WS,
        sandbox_config=deployment,
        host_store=host_store,
        **kw,
    )


def _host_starts(shim: VmShim) -> int:
    return len(shim.started_tokens)


async def test_starts_a_host_on_a_running_vm_and_links_it(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    host_id = await _ensure(deployment, host_store)
    assert shim.static_hosts[WS] == host_id
    host = host_store.get_host(host_id)
    assert host is not None and host.status == "online"
    # External row: the managed lifecycle can never reach the VM through it.
    assert host.sandbox_provider is None and host.sandbox_id is None
    assert host.name == WS
    assert not any(p.endswith("/resume") for p in shim.paths("POST"))
    assert shim.creates() == [] and shim.deletes_of_vms() == []


async def test_a_paused_vm_is_resumed_first(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    shim.vms[WS] = "paused"
    stages: list[str] = []
    await _ensure(deployment, host_store, on_stage=stages.append)
    # The static route wakes and waits; the ad hoc resume route is never used.
    assert f"/v1/static-sandboxes/{WS}/resume" in shim.paths("POST")
    assert f"/v1/sandboxes/{WS}/resume" not in shim.paths("POST")
    assert stages[0] == "waking" and "starting" in stages
    assert _host_starts(shim) == 1


async def test_a_vm_gone_during_resume_raises_gone(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    shim.vms[WS] = "paused"
    shim.resume_error = (409, "SANDBOX_GONE")
    with pytest.raises(OmnigentError, match="no longer exists") as exc:
        await _ensure(deployment, host_store)
    assert exc.value.code == ErrorCode.SANDBOX_GONE
    assert _host_starts(shim) == 0


async def test_a_resume_timeout_asks_to_retry(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    shim.vms[WS] = "paused"
    shim.resume_error = (504, "SANDBOX_RESUME_TIMEOUT")
    with pytest.raises(OmnigentError, match="Timed out trying to wake foo") as exc:
        await _ensure(deployment, host_store)
    assert exc.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert _host_starts(shim) == 0


async def test_no_second_start_while_the_host_is_online(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    first = await _ensure(deployment, host_store)
    second = await _ensure(deployment, host_store)
    assert first == second
    assert _host_starts(shim) == 1


async def test_concurrent_ensures_start_one_host(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    shim.vms[WS] = "paused"
    ids = await asyncio.gather(*(_ensure(deployment, host_store) for _ in range(4)))
    assert len(set(ids)) == 1
    assert _host_starts(shim) == 1
    assert shim.paths("POST").count(f"/v1/static-sandboxes/{WS}/resume") == 1


async def test_a_gone_vm_raises_and_nothing_is_created(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    del shim.vms[WS]  # the entry now reads as gone
    with pytest.raises(OmnigentError, match="no longer exists") as exc:
        await _ensure(deployment, host_store)
    assert exc.value.code == ErrorCode.SANDBOX_GONE
    assert shim.creates() == [] and _host_starts(shim) == 0
    # The single-entry read, never a list-and-filter.
    assert f"/v1/static-sandboxes/{WS}" in shim.paths("GET")
    assert "/v1/static-sandboxes" not in shim.paths("GET")


async def test_a_target_the_caller_does_not_own_is_404(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    shim.static_labels = {}
    with pytest.raises(OmnigentError) as exc:
        await _ensure(deployment, host_store)
    assert exc.value.http_status == 404
    assert _host_starts(shim) == 0


async def test_the_token_is_reminted_on_every_start(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    host_id = await _ensure(deployment, host_store)
    host_store.set_offline(host_id)
    await _ensure(deployment, host_store)
    first, second = shim.started_tokens
    assert first != second
    assert host_store.resolve_launch_token(host_id, first) is None
    assert host_store.resolve_launch_token(host_id, second) is not None


async def test_an_expired_token_heals_on_the_next_ensure(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    host_id = await _ensure(deployment, host_store)
    old = shim.started_tokens[-1]
    # Simulate expiry: the host can no longer reconnect with its token.
    host_store.upsert_static_host(
        host_id=host_id,
        name="static-sandbox-foo",
        user_id=OWNER,
        token=old,
        token_expires_at=now_epoch() - 1,
    )
    assert host_store.resolve_launch_token(host_id, old) is None
    await _ensure(deployment, host_store)
    assert host_store.resolve_launch_token(host_id, shim.started_tokens[-1]) is not None


async def test_a_host_that_never_connects_reports_the_log_path(
    shim: VmShim,
    deployment: ManagedSandboxDeployment,
    host_store: HostStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server import managed_hosts

    monkeypatch.setattr(managed_hosts, "MANAGED_HOST_ONLINE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(managed_hosts, "_ONLINE_POLL_INTERVAL_S", 0.01)
    shim.connect_on_start = False
    with pytest.raises(OmnigentError) as exc:
        await _ensure(deployment, host_store)
    assert exc.value.code == ErrorCode.HOST_START_FAILED and exc.value.http_status == 502
    assert "/tmp/omnigent-host.log on the VM" in str(exc.value)


async def test_the_linked_id_wins_over_a_local_candidate(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    shim.static_hosts[WS] = "0f6c1d3e-2a4b-4c5d-8e9f-0a1b2c3d4e5f"
    host_id = await _ensure(deployment, host_store)
    assert host_id == "0f6c1d3e2a4b4c5d8e9f0a1b2c3d4e5f"


async def test_reverse_lookup_only_asks_the_shim_about_candidates(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    host_id = await _ensure(deployment, host_store)
    host_store.set_offline(host_id)
    host = host_store.get_host(host_id)
    before = len(shim.requests)
    ref = await vm_hosts.find_static_host(host, deployment)
    assert ref is not None and ref.sandbox_id == WS and ref.owner == OWNER
    # One capabilities read and one list; a second lookup is cached and makes no call.
    assert await vm_hosts.find_static_host(host, deployment) == ref
    assert len(shim.requests) == before + 2
    # An ordinary external host (no launch token) never reaches the shim.
    host_store.upsert_on_connect(host_id="1" * 32, name="laptop", user_id=OWNER)
    assert await vm_hosts.find_static_host(host_store.get_host("1" * 32), deployment) is None
    assert len(shim.requests) == before + 2


async def test_stop_vm_host_stops_the_host_and_never_the_vm(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    host_id = await _ensure(deployment, host_store)
    token = shim.started_tokens[-1]
    removed = await vm_hosts.stop_vm_host(
        owner=OWNER,
        provider="remote",
        sandbox_id=WS,
        sandbox_config=deployment,
        host_store=host_store,
    )
    assert removed is True
    assert WS not in shim.static_labels
    assert any(remote_launcher._STOP_PREVIOUS_HOST_SCRIPT[:20] in s for s in shim.scripts())
    assert host_store.resolve_launch_token(host_id, token) is None
    assert host_store.get_host(host_id) is not None  # history still resolves
    assert shim.deletes_of_vms() == []


async def test_a_re_added_entry_gets_a_new_host_beside_the_old_one(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    host_id = await _ensure(deployment, host_store)
    await vm_hosts.stop_vm_host(
        owner=OWNER,
        provider="remote",
        sandbox_id=WS,
        sandbox_config=deployment,
        host_store=host_store,
    )
    shim.static_labels[WS] = "again"
    shim.static_hosts.pop(WS, None)
    new_id = await _ensure(deployment, host_store)
    # Only the entry links a host, never a name match.
    assert new_id != host_id and shim.static_hosts[WS] == new_id
    new_host = host_store.get_host(new_id)
    assert new_host is not None and new_host.name == f"{WS}-{new_id[:8]}"
    assert host_store.get_host(host_id) is not None


async def test_a_renamed_static_host_keeps_its_name(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    host_id = await _ensure(deployment, host_store)
    host_store.upsert_on_connect(host_id=host_id, name="my build box", user_id=OWNER)
    host_store.set_offline(host_id)
    assert await _ensure(deployment, host_store) == host_id
    host = host_store.get_host(host_id)
    assert host is not None and host.name == "my build box" and host.status == "online"


async def test_static_host_name_fits_the_column() -> None:
    assert vm_hosts.static_host_name("foo") == "foo"
    assert vm_hosts.static_host_name("foo", "ab" * 16) == "foo-abababab"
    for host_id in (None, "ab" * 16):
        long_name = vm_hosts.static_host_name("x" * 80, host_id)
        assert len(long_name) == 64
        assert long_name != vm_hosts.static_host_name("x" * 79, host_id)


async def test_providers_offering_static_sandboxes_come_from_the_capability(
    shim: VmShim, deployment: ManagedSandboxDeployment
) -> None:
    assert vm_hosts.static_sandbox_providers(None) == []
    assert vm_hosts.static_sandbox_providers(deployment) == ["remote"]
    # An older shim without the flag reads as off, once the cache expires.
    del shim.declared_capabilities["static_sandboxes"]
    assert vm_hosts.static_sandbox_providers(deployment) == ["remote"]
    vm_hosts._providers_cache.clear()
    assert vm_hosts.static_sandbox_providers(deployment) == []
    assert vm_hosts.static_sandboxes_configured(deployment) is True


async def test_a_provider_without_the_capability_is_refused(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    del shim.declared_capabilities["static_sandboxes"]
    with pytest.raises(OmnigentError, match="does not offer static sandboxes") as exc:
        await _ensure(deployment, host_store)
    assert exc.value.code == ErrorCode.INVALID_INPUT
    assert shim.static_hosts == {} and _host_starts(shim) == 0


async def test_an_unconfigured_or_non_static_provider_is_refused(
    shim: VmShim, deployment: ManagedSandboxDeployment
) -> None:
    with pytest.raises(OmnigentError, match="'modal' does not offer") as exc:
        vm_hosts.static_launcher(deployment, OWNER, "modal")
    assert exc.value.code == ErrorCode.INVALID_INPUT
    plain = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="http://omnigent.mesh:8080",
            launcher_factory=_PlainLauncher,
            token_ttl_s=3600,
            provider="plain",
        )
    )
    with pytest.raises(OmnigentError, match="'plain' does not offer"):
        vm_hosts.static_launcher(plain, OWNER, "plain")
    assert vm_hosts.static_sandboxes_configured(plain) is False
    assert vm_hosts.static_sandbox_providers(plain) == []


async def test_reverse_lookup_skips_a_provider_without_the_capability(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    host_id = await _ensure(deployment, host_store)
    host_store.set_offline(host_id)
    vm_hosts._lookup_cache.clear()
    vm_hosts._providers_cache.clear()
    del shim.declared_capabilities["static_sandboxes"]
    assert await vm_hosts.find_static_host(host_store.get_host(host_id), deployment) is None


async def test_a_timeout_reports_the_vm_diagnostics(
    shim: VmShim,
    deployment: ManagedSandboxDeployment,
    host_store: HostStore,
    fast_wait: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    shim.connect_on_start = False
    shim.diagnostics = {"log_tail": "omnigent host exited (1); attempt 3"}
    with pytest.raises(OmnigentError) as exc, caplog.at_level("WARNING"):
        await _ensure(deployment, host_store)
    detail = str(exc.value)
    assert "did not come online" in detail
    assert "omnigent host exited (1); attempt 3" in detail and "omnigent 0.17.0" in detail
    # Structured diagnostics from the provider, for this server's URL; no script exec.
    assert shim.body(f"/v1/static-sandboxes/{WS}/diagnostics") == {
        "server_url": "http://omnigent.mesh:8080"
    }
    assert not any("tail -n" in script for script in shim.scripts())
    assert any(
        "did not come online" in r.getMessage() and WS in r.getMessage() for r in caplog.records
    )


async def test_the_error_keeps_only_a_short_log_tail(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore, fast_wait: None
) -> None:
    shim.connect_on_start = False
    shim.diagnostics = {"log_tail": "x" * 5000 + "\nlast line"}
    with pytest.raises(OmnigentError) as exc:
        await _ensure(deployment, host_store)
    detail = str(exc.value)
    assert len(detail) < 1800 and detail.endswith("last line")


async def test_a_fatal_reason_fails_fast(
    shim: VmShim,
    deployment: ManagedSandboxDeployment,
    host_store: HostStore,
    fast_wait: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.server import managed_hosts

    # Long enough that only the probe can end the wait in time.
    monkeypatch.setattr(managed_hosts, "MANAGED_HOST_ONLINE_TIMEOUT_S", 30)
    shim.connect_on_start = False
    shim.diagnostics = {
        "host_process_running": False,
        "fatal_reason": "the omnigent host process is not running on the VM",
        "log_tail": "\u2717 Could not connect to http://omnigent.mesh:8080.",
    }
    loop = asyncio.get_running_loop()
    started = loop.time()
    with pytest.raises(OmnigentError) as exc:
        await _ensure(deployment, host_store)
    assert loop.time() - started < 10
    detail = str(exc.value)
    assert (
        "stopped before coming online: the omnigent host process is not running on the VM"
        in detail
    )
    assert detail.count("the omnigent host process is not running") == 1
    assert "Could not connect" in detail


async def test_a_vm_that_cannot_resolve_the_server_fails_fast_and_says_why(
    shim: VmShim,
    deployment: ManagedSandboxDeployment,
    host_store: HostStore,
    fast_wait: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistent VM started outside the shim has no route to the in-mesh server."""

    from omnigent.server import managed_hosts

    monkeypatch.setattr(managed_hosts, "MANAGED_HOST_ONLINE_TIMEOUT_S", 30)
    shim.connect_on_start = False
    shim.diagnostics = {
        "server_resolves": False,
        "fatal_reason": "server host omnigent.mesh does not resolve on the VM; the VM cannot "
        "reach the Omnigent server (its egress allowlist lacks this host)",
    }
    with pytest.raises(OmnigentError) as exc:
        await _ensure(deployment, host_store)
    assert "egress allowlist lacks this host" in str(exc.value)


@pytest.mark.parametrize(
    ("error", "note"),
    [
        ((409, "SANDBOX_PAUSED"), "(no diagnostics: foo is not running)"),
        ((502, "REMOTE_EXEC_FAILED"), "(could not read diagnostics from the VM"),
    ],
)
async def test_unreadable_diagnostics_never_mask_the_timeout(
    shim: VmShim,
    deployment: ManagedSandboxDeployment,
    host_store: HostStore,
    fast_wait: None,
    error: tuple[int, str],
    note: str,
) -> None:
    shim.connect_on_start = False
    shim.diagnostics_error = error
    with pytest.raises(OmnigentError) as exc:
        await _ensure(deployment, host_store)
    assert exc.value.code == ErrorCode.HOST_START_FAILED
    assert "did not come online" in str(exc.value) and note in str(exc.value)


async def test_callers_queued_behind_a_failed_start_do_not_retry_it(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore, fast_wait: None
) -> None:
    shim.connect_on_start = False
    shim.diagnostics = {"fatal_reason": "the omnigent host process is not running on the VM"}
    results = await asyncio.gather(
        *(_ensure(deployment, host_store) for _ in range(3)), return_exceptions=True
    )
    assert all(
        isinstance(r, OmnigentError) and r.code == ErrorCode.HOST_START_FAILED for r in results
    )
    assert _host_starts(shim) == 1
    # A fresh call afterwards does try again.
    shim.connect_on_start = True
    await _ensure(deployment, host_store)
    assert _host_starts(shim) == 2


async def test_a_reserved_user_never_reaches_the_shim(
    shim: VmShim, deployment: ManagedSandboxDeployment, host_store: HostStore
) -> None:
    before = len(shim.requests)
    with pytest.raises(OmnigentError, match="signed-in user") as exc:
        await vm_hosts.ensure_vm_host(
            owner="local",
            provider="remote",
            sandbox_id=WS,
            sandbox_config=deployment,
            host_store=host_store,
        )
    assert exc.value.code == ErrorCode.INVALID_INPUT
    host_store.upsert_on_connect(host_id="1" * 32, name="laptop", user_id="local")
    assert await vm_hosts.find_static_host(host_store.get_host("1" * 32), deployment) is None
    assert len(shim.requests) == before
