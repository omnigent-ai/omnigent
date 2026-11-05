"""The registry default for recording a session's MCP services."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from omnigent.errors import OmnigentError
from omnigent.server.session_mcp_selection import (
    MAX_SESSION_MCP_SERVICES,
    RegistryMcpSelection,
    session_mcp_selection,
)
from omnigent.spec.types import AgentSpec, MCPServerConfig


def _request(service_ids: set[str]) -> SimpleNamespace:
    class _Registry:
        def service(self, service_id: str, user_id: str) -> object:
            from omnigent.server.routes.connections_base import ConnectionError

            if service_id not in service_ids:
                raise ConnectionError(f"MCP service {service_id!r} is not available")
            return object()

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
