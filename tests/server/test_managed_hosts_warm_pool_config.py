"""Warm-pool configuration and pre-allocation request context."""

from __future__ import annotations

from unittest.mock import Mock

import click
import pytest
from fastapi import HTTPException

from omnigent.db.utils import now_epoch
from omnigent.onboarding.sandboxes.agent_sandbox import AgentSandboxLauncher
from omnigent.server.managed_hosts import (
    ManagedSandboxConfig,
    ManagedSandboxDeployment,
    _register_and_start_host,
    launch_managed_host,
    parse_sandbox_config,
    relaunch_managed_host,
    resume_managed_host,
)
from omnigent.stores.host_store import HostStore
from tests.server.helpers import FakeSandboxLauncher, HostStartInvocation


def _config(**fields: object) -> dict[str, object]:
    return {
        "provider": "agent_sandbox",
        "server_url": "https://server.example.com",
        **fields,
    }


def test_warm_pool_factory_preserves_kubernetes_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.onboarding.sandboxes import agent_sandbox_warm_pool

    constructor = Mock(return_value=FakeSandboxLauncher())
    monkeypatch.setattr(agent_sandbox_warm_pool, "AgentSandboxWarmPoolLauncher", constructor)
    deployment = parse_sandbox_config(
        _config(
            agent_sandbox={"warm_pool": " hosts.v1 "},
            kubernetes={
                "namespace": "sandbox-lab",
                "image": "example.com/host:pool-v1",
                "secret_name": "operator-credentials",
                "node_selector": {"kubernetes.io/arch": "arm64"},
                "resources": {"requests": {"cpu": "250m", "memory": "512Mi"}},
                "in_cluster": True,
            },
        )
    )
    assert deployment is not None
    deployment.default.launcher_factory()
    assert constructor.call_count == 1
    arguments = constructor.call_args.kwargs
    assert arguments["warm_pool"] == "hosts.v1"
    assert arguments["namespace"] == "sandbox-lab"
    assert arguments["image"] == "example.com/host:pool-v1"
    assert arguments["secret_name"] == "operator-credentials"
    assert arguments["node_selector"] == {"kubernetes.io/arch": "arm64"}
    assert arguments["resources"] == {"requests": {"cpu": "250m", "memory": "512Mi"}}
    assert arguments["in_cluster"] is True


@pytest.mark.parametrize("section", [None, {}, {"warm_pool": None}])
def test_disabled_pool_keeps_warm_capable_launcher_with_direct_provisioning(
    section: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    from omnigent.onboarding.sandboxes.agent_sandbox_warm_pool import (
        AgentSandboxWarmPoolLauncher,
    )

    direct_provision = Mock(return_value="direct-sandbox")
    monkeypatch.setattr(AgentSandboxLauncher, "provision", direct_provision)
    deployment = parse_sandbox_config(_config(agent_sandbox=section))
    assert deployment is not None
    launcher = deployment.default.launcher_factory()
    assert isinstance(launcher, AgentSandboxWarmPoolLauncher)
    launcher.prepare_for_launch(agent_name="code")
    assert launcher.provision("managed-demo") == "direct-sandbox"
    direct_provision.assert_called_once_with("managed-demo")


def test_omitted_pool_keeps_warm_capable_launcher() -> None:
    from omnigent.onboarding.sandboxes.agent_sandbox_warm_pool import (
        AgentSandboxWarmPoolLauncher,
    )

    deployment = parse_sandbox_config(_config())
    assert deployment is not None
    assert isinstance(deployment.default.launcher_factory(), AgentSandboxWarmPoolLauncher)


@pytest.mark.parametrize(
    "section",
    [
        "hosts-v1",
        {"warm_pools": "hosts-v1"},
        {"warm_pool": {"name": "hosts-v1"}},
        {"warm_pool": True},
        {"warm_pool": ""},
        {"warm_pool": "   "},
        {"warm_pool": "Hosts_V1"},
        {"warm_pool": "hosts/v1"},
        {"warm_pool": "-hosts"},
        {"warm_pool": "hosts."},
        {"warm_pool": "hosts..v1"},
        {"warm_pool": "a" * 254},
    ],
)
def test_invalid_warm_pool_config_fails_at_parse_time(section: object) -> None:
    with pytest.raises(ValueError, match=r"sandbox\.agent_sandbox"):
        parse_sandbox_config(_config(agent_sandbox=section))


@pytest.mark.parametrize("provider", ["kubernetes", "modal"])
def test_other_providers_reject_agent_sandbox_options(provider: str) -> None:
    with pytest.raises(ValueError, match="requires provider: agent_sandbox"):
        parse_sandbox_config(_config(provider=provider, agent_sandbox={"warm_pool": "hosts-v1"}))


def test_warm_pool_config_in_multi_provider_entry() -> None:
    deployment = parse_sandbox_config(
        {
            "server_url": "https://server.example.com",
            "providers": [
                {"provider": "kubernetes"},
                {"provider": "agent_sandbox", "agent_sandbox": {"warm_pool": "hosts-v1"}},
            ],
        }
    )
    assert deployment is not None
    assert deployment.launchable_providers() == ("kubernetes", "agent_sandbox")


async def test_request_context_precedes_launch_relaunch_and_resume(db_uri: str) -> None:
    host_store = HostStore(db_uri)
    calls: list[tuple[str, str | None]] = []

    class ContextLauncher(FakeSandboxLauncher):
        def prepare_for_launch(self, *, agent_name: str | None = None) -> None:
            calls.append(("context", agent_name))

        def prepare(self) -> None:
            calls.append(("prepare", None))
            super().prepare()

        def provision(self, name: str) -> str:
            calls.append(("provision", None))
            return super().provision(name)

        def resume(self, sandbox_id: str) -> None:
            calls.append(("resume", None))
            super().resume(sandbox_id)

    def register(invocation: HostStartInvocation) -> None:
        owner = host_store.resolve_launch_token(invocation.host_id, invocation.token)
        assert owner is not None and owner.user_id == "pool-user"
        host_store.upsert_on_connect(
            host_id=invocation.host_id, name=invocation.host_name, user_id="pool-user"
        )

    launcher = ContextLauncher(on_host_start=register, can_resume=True)
    deployment = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://server.example.com",
            launcher_factory=lambda: launcher,
            token_ttl_s=3600,
        )
    )
    first = await launch_managed_host(
        config=deployment, owner="pool-user", host_store=host_store, agent_name="code"
    )
    assert calls == [("context", "code"), ("prepare", None), ("provision", None)]

    calls.clear()
    host = host_store.get_host(first.host_id)
    assert host is not None
    await relaunch_managed_host(
        config=deployment, host=host, host_store=host_store, agent_name="research"
    )
    assert calls == [("context", "research"), ("prepare", None), ("provision", None)]

    calls.clear()
    await resume_managed_host(first.host_id, host_store, deployment, force=True, agent_name="code")
    assert calls == [("context", "code"), ("resume", None)]


async def test_rejected_launch_context_does_not_allocate(db_uri: str) -> None:
    class IncompatibleLauncher(FakeSandboxLauncher):
        def prepare_for_launch(self, *, agent_name: str | None = None) -> None:
            raise click.ClickException("pool does not match the requested agent")

    launcher = IncompatibleLauncher()
    deployment = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://server.example.com",
            launcher_factory=lambda: launcher,
            token_ttl_s=3600,
        )
    )
    with pytest.raises(HTTPException, match="pool does not match") as error:
        await launch_managed_host(
            config=deployment,
            owner="pool-user",
            host_store=HostStore(db_uri),
            agent_name="code",
        )
    assert error.value.status_code == 502
    assert not launcher.prepared
    assert launcher.provisioned_names == []


async def test_registration_failure_terminates_allocated_sandbox(
    db_uri: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    host_store = HostStore(db_uri)
    failure = RuntimeError("database registration failed")
    monkeypatch.setattr(host_store, "register_managed_host", Mock(side_effect=failure))
    launcher = FakeSandboxLauncher()
    deployment = ManagedSandboxDeployment.single(
        ManagedSandboxConfig(
            server_url="https://server.example.com",
            launcher_factory=lambda: launcher,
            token_ttl_s=3600,
        )
    )

    with pytest.raises(HTTPException) as caught:
        await launch_managed_host(config=deployment, owner="pool-user", host_store=host_store)

    assert caught.value.status_code == 502
    assert caught.value.__cause__ is failure
    assert "database registration failed" in caught.value.detail
    assert len(launcher.provisioned_names) == 1
    assert launcher.terminated == ["sb-fake-1"]
    assert launcher.host_starts == []


@pytest.mark.parametrize("keep_host", [False, True])
@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_registration_failure_preserves_existing_host_and_original_error(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
    keep_host: bool,
    cleanup_fails: bool,
) -> None:
    host_store = HostStore(db_uri)
    existing = host_store.register_managed_host(
        host_id="910fe2b984a44a298221cd8be8ea08f5",
        name="managed-existing",
        user_id="pool-user",
        token="existing-token",
        provider="modal",
        sandbox_id="existing-sandbox",
        token_expires_at=now_epoch() + 3600,
    )
    failure = ValueError("host lifecycle conflicted during registration")
    operation = "replace_managed_host_sandbox" if keep_host else "register_managed_host"
    monkeypatch.setattr(host_store, operation, Mock(side_effect=failure))

    class CleanupLauncher(FakeSandboxLauncher):
        def terminate(self, sandbox_id: str) -> None:
            super().terminate(sandbox_id)
            if cleanup_fails:
                raise RuntimeError("provider cleanup unavailable")

    launcher = CleanupLauncher()
    config = ManagedSandboxConfig(
        server_url="https://server.example.com",
        launcher_factory=lambda: launcher,
        token_ttl_s=3600,
    )
    with pytest.raises(ValueError) as caught:
        await _register_and_start_host(
            launcher=launcher,
            config=config,
            host_store=host_store,
            host_id=existing.host_id,
            host_name=existing.name,
            owner=existing.user_id,
            sandbox_id="newly-allocated-sandbox",
            keep_host_on_failure=keep_host,
        )

    assert caught.value is failure
    assert launcher.terminated == ["newly-allocated-sandbox"]
    assert launcher.host_starts == []
    assert host_store.get_host(existing.host_id) == existing
    assert host_store.resolve_launch_token(existing.host_id, "existing-token") is not None
