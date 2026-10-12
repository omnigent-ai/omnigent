"""Catalog declarations survive spec translation without creating local transports."""

from __future__ import annotations

import pytest

from omnigent.inner.loader import load_agent_def
from omnigent.runner.mcp_manager import RunnerMcpManager
from omnigent.server.routes.session_mcp_servers import _body_to_file_yaml, _body_to_inline_yaml
from omnigent.server.schemas import UpsertMCPServerRequest
from omnigent.spec.omnigent import agent_def_to_agent_spec, agent_spec_to_agent_def
from omnigent.spec.parser import _parse_registry_mcp_server


def registry_spec():
    return agent_def_to_agent_spec(
        load_agent_def(
            {
                "name": "catalog-agent",
                "executor": {"model": "gpt-4o-mini", "harness": "openai-agents"},
                "tools": {
                    "tracker": {"type": "mcp", "transport": "registry", "tools": ["whoami"]}
                },
            }
        )
    )


def test_registry_round_trip_keeps_reference_and_tool_restriction():
    spec = registry_spec()
    config = spec.mcp_servers[0]
    assert config.name == "tracker"
    assert config.transport == "registry"
    assert config.tools == ["whoami"]
    assert config.url is None
    assert config.command is None
    restored = agent_def_to_agent_spec(agent_spec_to_agent_def(spec))
    assert restored.mcp_servers == spec.mcp_servers


@pytest.mark.parametrize("override", ["url", "command", "env", "headers", "auth", "profile"])
def test_inner_registry_rejects_connection_overrides(override):
    with pytest.raises(ValueError, match="cannot override"):
        load_agent_def(
            {
                "name": "catalog-agent",
                "tools": {
                    "tracker": {"type": "mcp", "transport": "registry", override: "override"}
                },
            }
        )


def test_native_registry_keeps_an_empty_allowlist():
    config = _parse_registry_mcp_server("tracker", {"tools": []}, "test")
    assert config.tools == []


@pytest.mark.parametrize("serialize", [_body_to_file_yaml, _body_to_inline_yaml])
@pytest.mark.parametrize("allowed", [[], ["whoami"]])
def test_editing_a_registry_reference_preserves_its_tool_restriction(serialize, allowed):
    body = UpsertMCPServerRequest(name="tracker", transport="registry", description="Updated")
    result = serialize(body, {"tools": allowed})
    assert result["tools"] == allowed


@pytest.mark.asyncio
async def test_runner_does_not_start_a_local_transport_for_catalog_services():
    manager = RunnerMcpManager()
    spec = registry_spec()
    await manager.prewarm(spec)
    result = await manager.schemas_for(spec)
    assert result.schemas == []
    assert result.failures == {}


def test_registry_selection_is_isolated_and_preserves_authored_restrictions():
    from omnigent.mcp_selection import registry_selection_labels, selected_mcp_spec
    from omnigent.server.mcp_compatibility import registry_services
    from omnigent.spec.types import AgentSpec, MCPServerConfig

    source = registry_spec()
    source.sub_agents = [
        AgentSpec(
            spec_version=1,
            name="child",
            mcp_servers=[MCPServerConfig(name="nested", transport="registry")],
        )
    ]
    services = ["service" + str(i).zfill(33) for i in range(40)]
    labels = registry_selection_labels(source, services)
    assert all(len(k) <= 128 and len(v) <= 256 for k, v in labels.items())
    selected = selected_mcp_spec(source, labels)
    assert registry_services(selected) == {"tracker", "nested", *services}
    assert selected.mcp_servers[0].tools == ["whoami"]
    assert registry_services(source) == {"tracker", "nested"}
    assert registry_services(selected_mcp_spec(source, dict.fromkeys(labels, "0"))) == {
        "tracker",
        "nested",
    }
    for transport in ["http", "stdio"]:
        plain = AgentSpec(
            spec_version=1,
            name="custom",
            mcp_servers=[MCPServerConfig(name="custom", transport=transport)],
        )
        assert not registry_services(plain)
