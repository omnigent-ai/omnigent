"""MCP credential-broker integration (plan Tasks 13a-c).

MCP servers spawn unsandboxed in the parent, so the agent can't read their env;
the broker's value here is keeping long-lived creds out of static MCP config +
ephemeral per-connect resolution. These tests cover the parent-side resolution
and the spec→connection threading (cache-key + extraction), not a live spawn.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.errors import OmnigentError
from omnigent.inner.datamodel import (
    CredentialBrokerField,
    CredentialBrokerGroup,
    CredentialBrokerSpec,
    CredentialSourceSpec,
)
from omnigent.runner.mcp_manager import _agent_credential_broker, compute_spec_hash
from omnigent.spec.parser import _parse_stdio_mcp_server
from omnigent.spec.types import MCPServerConfig
from omnigent.tools.mcp import McpServerConnection


def _broker() -> CredentialBrokerSpec:
    return CredentialBrokerSpec(
        groups={
            "pg": CredentialBrokerGroup(
                fields=[
                    CredentialBrokerField(
                        env="PGHOST",
                        fallback=CredentialSourceSpec(kind="command", command="printf h"),
                    )
                ]
            )
        },
        tools={},
    )


def _conn(groups, broker) -> McpServerConnection:
    cfg = MCPServerConfig(name="x", transport="stdio", command="true", credential_groups=groups)
    return McpServerConnection(config=cfg, credential_broker=broker)


def test_resolve_broker_env_merges_resolved_creds():
    assert _conn(["pg"], _broker())._resolve_broker_env() == {"PGHOST": "h"}


def test_resolve_broker_env_empty_without_groups():
    assert _conn([], _broker())._resolve_broker_env() == {}


def test_resolve_broker_env_skips_when_no_broker():
    # credential_groups set but agent has no broker -> warn + ignore (not crash).
    assert _conn(["pg"], None)._resolve_broker_env() == {}


def test_resolve_broker_env_unknown_group_raises():
    with pytest.raises(RuntimeError, match="unknown broker groups"):
        _conn(["nope"], _broker())._resolve_broker_env()


def test_agent_credential_broker_extraction():
    b = _broker()
    spec = SimpleNamespace(os_env=SimpleNamespace(sandbox=SimpleNamespace(credential_broker=b)))
    assert _agent_credential_broker(spec) is b
    assert _agent_credential_broker(SimpleNamespace(os_env=None)) is None
    assert _agent_credential_broker(SimpleNamespace()) is None


def test_compute_spec_hash_invalidates_on_broker_change():
    cfgs = [MCPServerConfig(name="x", transport="stdio", command="true")]
    assert compute_spec_hash(cfgs) != compute_spec_hash(cfgs, credential_broker=_broker())


def test_parser_stdio_credential_groups():
    cfg = _parse_stdio_mcp_server(
        "x", {"command": "npx", "credential_groups": ["pg"]}, Path("/x.yaml"), expand_env=False
    )
    assert cfg.credential_groups == ["pg"]


def test_parser_stdio_credential_groups_must_be_list_of_str():
    with pytest.raises(OmnigentError, match="credential_groups"):
        _parse_stdio_mcp_server(
            "x", {"command": "npx", "credential_groups": "pg"}, Path("/x.yaml"), expand_env=False
        )
