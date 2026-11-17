"""How the server records a session's MCP services: the registry, or a provider's own."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment
from omnigent.server.mcp_registry import McpRegistry, McpRegistryConfig, McpService
from omnigent.server.routes._sessions.orchestration import _inherited_mcp_labels
from omnigent.server.session_mcp_selection import (
    MAX_SESSION_MCP_SERVICES,
    RegistryMcpSelection,
    host_sandbox,
    mcp_selection_for,
    task_sandbox,
)
from omnigent.spec.types import AgentSpec, MCPServerConfig
from omnigent.stores.credential_store.sqlalchemy_store import CredentialStore
from tests.server.test_credential_store import _FakeCipher


def _request(service_ids: set[str]) -> SimpleNamespace:
    class _Registry:
        services = {service_id: object() for service_id in service_ids}

        def service(self, service_id: str, user_id: str) -> object:
            from omnigent.server.routes.connections_base import ConnectionError

            if service_id not in service_ids:
                raise ConnectionError(f"MCP service {service_id!r} is not available")
            return self.services[service_id]

        def connected(self, service: object, user_id: str, app_state: object) -> bool:
            return True

    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(mcp_registry=_Registry())))


def test_labels_round_trip_through_the_registry_prefix() -> None:
    selection = RegistryMcpSelection()
    labels = selection.session_labels(["github", "linear"])
    assert labels == {"omnigent.mcp_service.github": "1", "omnigent.mcp_service.linear": "1"}
    # A "0" removes a spec-declared service, so it is not part of the selection.
    labels["omnigent.mcp_service.slack"] = "0"
    labels["other.label"] = "1"
    assert selection.selected_services(labels) == ("github", "linear")


def test_validate_deduplicates_and_checks_each_service() -> None:
    selection = RegistryMcpSelection()
    request = _request({"github", "linear"})
    assert selection.validate(
        request, ["github", "linear", "github"], spec=None, user_id="alice"
    ) == ("github", "linear")
    with pytest.raises(OmnigentError, match="not available"):
        selection.validate(request, ["slack"], spec=None, user_id="alice")
    with pytest.raises(OmnigentError, match="Invalid MCP service id"):
        selection.validate(request, ["System.AI.github"], spec=None, user_id="alice")
    with pytest.raises(OmnigentError, match="at most"):
        selection.validate(
            request,
            [f"s{i}" for i in range(MAX_SESSION_MCP_SERVICES + 1)],
            spec=None,
            user_id="alice",
        )


def test_validate_rejects_a_name_the_agent_uses_for_a_custom_server() -> None:
    spec = AgentSpec(
        spec_version=1,
        name="agent",
        mcp_servers=[MCPServerConfig(name="github", transport="http")],
    )
    with pytest.raises(OmnigentError, match="custom transport"):
        RegistryMcpSelection().validate(
            _request({"github"}), ["github"], spec=spec, user_id="alice"
        )


def _provider(name: str, selection: object | None = None) -> ManagedSandboxConfig:
    return ManagedSandboxConfig(
        server_url="https://omnigent.example.com",
        launcher_factory=lambda: None,  # type: ignore[arg-type, return-value]
        token_ttl_s=60,
        provider=name,
        mcp_selection=selection,  # type: ignore[arg-type]
    )


class _Hosts:
    """Host rows by id, each naming its sandbox provider or ``None`` for a connected host."""

    def __init__(self, providers: dict[str, str | None]) -> None:
        self._providers = providers

    def get_host(self, host_id: str) -> SimpleNamespace | None:
        if host_id not in self._providers:
            return None
        return SimpleNamespace(sandbox_provider=self._providers[host_id])


def _state(
    deployment: ManagedSandboxDeployment | None, hosts: dict[str, str | None]
) -> SimpleNamespace:
    return SimpleNamespace(sandbox_config=deployment, host_store=_Hosts(hosts))


def test_a_provider_selection_applies_only_in_that_providers_sandboxes() -> None:
    custom = object()
    deployment = ManagedSandboxDeployment(configs=(_provider("box", custom), _provider("plain")))
    state = _state(
        deployment,
        {"box-host": "box", "plain-host": "plain", "laptop": None, "gone": "removed"},
    )

    assert mcp_selection_for(task_sandbox(deployment, "managed_sandbox")) is custom
    assert mcp_selection_for(host_sandbox(state, "box-host")) is custom
    # Connected hosts, providers that set none, and unknown hosts or providers use the registry.
    for sandbox in (
        task_sandbox(deployment, "connected_host"),
        task_sandbox(None, "managed_sandbox"),
        host_sandbox(state, "plain-host"),
        host_sandbox(state, "laptop"),
        host_sandbox(state, "gone"),
        host_sandbox(state, "missing"),
        host_sandbox(state, None),
        host_sandbox(_state(None, {"box-host": "box"}), "box-host"),
    ):
        assert isinstance(mcp_selection_for(sandbox), RegistryMcpSelection)


def test_validate_rejects_an_account_the_owner_has_not_connected(db_uri) -> None:
    store = CredentialStore(db_uri, _FakeCipher())
    entry = McpService.model_validate(
        {
            "id": "tracker",
            "title": "Tracker",
            "url": "https://mcp.example.test/mcp",
            "tools": ["read_ticket"],
            "auth": "bearer",
        }
    )
    registry = McpRegistry(McpRegistryConfig(services=[entry]), store)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(mcp_registry=registry)))
    selection = RegistryMcpSelection()

    store.upsert("alice", "mcp:tracker", secret={"access_token": "t"}, metadata={})
    assert selection.validate(request, ["tracker"], spec=None, user_id="alice") == ("tracker",)
    store.delete("alice", "mcp:tracker")
    with pytest.raises(OmnigentError, match="Connect your tracker account") as raised:
        selection.validate(request, ["tracker"], spec=None, user_id="alice")
    assert raised.value.code == ErrorCode.FORBIDDEN


class _PerServiceSelection:
    """A deployment that writes one label per service under its own prefix."""

    def __init__(self, rejected: frozenset[str] = frozenset()) -> None:
        self.rejected = rejected

    def validate(self, request, services, *, spec, user_id):
        if self.rejected & set(services):
            raise OmnigentError("revoked", code=ErrorCode.FORBIDDEN)
        return tuple(services)

    def session_labels(self, services):
        return {f"deployment.mcp.{service}": "1" for service in services}

    def selected_services(self, labels):
        return tuple(
            key.removeprefix("deployment.mcp.")
            for key in labels
            if key.startswith("deployment.mcp.")
        )


_SPEC = AgentSpec(spec_version=1, name="agent")


async def _inherit(
    selection,
    worker_labels,
    *,
    spec=_SPEC,
    source_labels=None,
    worker_host_id=None,
    new_sandbox=False,
    new_sandbox_provider=None,
):
    # The source runs in a "box" sandbox, whose provider records services its own way.
    deployment = ManagedSandboxDeployment(
        configs=(_provider("box", selection), _provider("plain"))
    )
    state = _state(
        deployment,
        {"source-host": "box", "box-host": "box", "plain-host": "plain", "laptop": None},
    )
    return await _inherited_mcp_labels(
        SimpleNamespace(app=SimpleNamespace(state=state)),
        {"deployment.mcp.source-only": "1"} if source_labels is None else source_labels,
        worker_labels,
        spec=spec,
        user_id="alice",
        host_id=worker_host_id,
        runner_id=None,
        source_host_id="source-host",
        new_sandbox_provider=new_sandbox_provider,
        new_sandbox=new_sandbox,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("worker", "where"),
    [
        ({}, "on its source's host"),
        ({"worker_host_id": "box-host"}, "on another host of the provider"),
        ({"new_sandbox": True}, "in a new sandbox of the default provider"),
        ({"new_sandbox": True, "new_sandbox_provider": "box"}, "in a new sandbox it names"),
    ],
    ids=["same-host", "existing-sandbox", "new-default-sandbox", "new-named-sandbox"],
)
async def test_worker_in_the_sources_provider_inherits_the_source_selection(worker, where) -> None:
    assert await _inherit(_PerServiceSelection(), {}, **worker) == {
        "deployment.mcp.source-only": "1"
    }, where


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "worker",
    [
        {"worker_host_id": "laptop"},
        {"worker_host_id": "plain-host"},
        {"new_sandbox": True, "new_sandbox_provider": "plain"},
    ],
    ids=["connected-host", "other-provider-host", "other-provider-new-sandbox"],
)
async def test_worker_elsewhere_does_not_get_labels_its_provider_cannot_read(worker) -> None:
    # The registry reads none of the source provider's labels, so nothing is inherited.
    assert await _inherit(_PerServiceSelection(), {}, **worker) == {}


@pytest.mark.asyncio
async def test_worker_with_its_own_selection_inherits_nothing() -> None:
    # The keys differ, so a key-overlap check would merge the source's tools in.
    assert await _inherit(_PerServiceSelection(), {"deployment.mcp.worker-only": "1"}) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("selection", "spec"),
    [(_PerServiceSelection(frozenset({"source-only"})), _SPEC), (_PerServiceSelection(), None)],
    ids=["unusable-service", "agent-not-loaded"],
)
async def test_worker_drops_a_selection_it_cannot_check(selection, spec) -> None:
    assert await _inherit(selection, {}, spec=spec) == {}
