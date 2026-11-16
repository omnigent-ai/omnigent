"""The registry default for recording a session's MCP services."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.mcp_registry import McpRegistry, McpRegistryConfig, McpService
from omnigent.server.routes._sessions.orchestration import _inherited_mcp_labels
from omnigent.server.session_mcp_selection import (
    MAX_SESSION_MCP_SERVICES,
    RegistryMcpSelection,
    session_mcp_selection,
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


def test_deployment_selection_replaces_the_default() -> None:
    custom = object()
    assert session_mcp_selection(SimpleNamespace(session_mcp_selection=custom)) is custom
    assert isinstance(session_mcp_selection(SimpleNamespace()), RegistryMcpSelection)
    assert isinstance(session_mcp_selection(None), RegistryMcpSelection)


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


async def _inherit(selection, worker_labels, *, spec=_SPEC):
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(session_mcp_selection=selection))
    )
    return await _inherited_mcp_labels(
        request,
        {"deployment.mcp.source-only": "1"},
        worker_labels,
        spec=spec,
        user_id="alice",
        host_id=None,
        runner_id=None,
    )


@pytest.mark.asyncio
async def test_worker_inherits_the_source_selection() -> None:
    assert await _inherit(_PerServiceSelection(), {}) == {"deployment.mcp.source-only": "1"}


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
