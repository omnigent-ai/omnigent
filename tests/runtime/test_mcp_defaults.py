"""Tests for the shared MCP-defaults helper (#4): wire (de)serialization and
the dedup-by-name merge used to fold server-wide default MCP servers into a
session spec on the runner.
"""

from __future__ import annotations

from omnigent.runtime.mcp_defaults import (
    deserialize_mcp_servers,
    merge_default_mcp_servers,
    serialize_mcp_servers,
)
from omnigent.spec.types import AgentSpec, MCPServerConfig


def _http(name: str, url: str = "https://x/sse") -> MCPServerConfig:
    return MCPServerConfig(
        name=name, transport="http", url=url, headers={"Authorization": "Bearer t"}
    )


def _stdio(name: str) -> MCPServerConfig:
    return MCPServerConfig(
        name=name, transport="stdio", command="npx", args=["-y", "srv"], env={"K": "v"}
    )


def _spec(*servers: MCPServerConfig) -> AgentSpec:
    return AgentSpec(spec_version="1.0", mcp_servers=list(servers))


def test_serialize_roundtrip_http_and_stdio() -> None:
    back = deserialize_mcp_servers(serialize_mcp_servers([_http("search"), _stdio("fs")]))
    assert [s.name for s in back] == ["search", "fs"]
    assert back[0].transport == "http"
    assert back[0].url == "https://x/sse"
    assert back[0].headers == {"Authorization": "Bearer t"}
    assert back[1].transport == "stdio"
    assert back[1].command == "npx"
    assert back[1].args == ["-y", "srv"]
    assert back[1].env == {"K": "v"}


def test_merge_appends_missing_defaults() -> None:
    out = merge_default_mcp_servers(_spec(_http("agent_own")), [_http("shared")])
    assert [s.name for s in out.mcp_servers] == ["agent_own", "shared"]


def test_merge_dedups_with_agent_winning() -> None:
    spec = _spec(_http("dup", "https://agent/sse"))
    out = merge_default_mcp_servers(spec, [_http("dup", "https://default/sse"), _http("extra")])
    assert [s.name for s in out.mcp_servers] == ["dup", "extra"]
    # The agent's own "dup" is kept; the same-named default is dropped.
    dup = next(s for s in out.mcp_servers if s.name == "dup")
    assert dup.url == "https://agent/sse"


def test_merge_is_noop_and_never_mutates_input() -> None:
    spec = _spec(_http("a"))
    # Nothing to add → the very same object is returned (no needless copy).
    assert merge_default_mcp_servers(spec, []) is spec
    assert merge_default_mcp_servers(spec, [_http("a")]) is spec
    # Appending returns a copy and leaves the input spec untouched.
    before = list(spec.mcp_servers)
    out = merge_default_mcp_servers(spec, [_http("b")])
    assert out is not spec
    assert spec.mcp_servers == before
