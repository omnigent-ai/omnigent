"""Tests for ``parse_default_mcp_servers`` — the server-config ``mcp_servers:``
parser that backs the shared MCP registry (#4). Mirrors the inline ``type: mcp``
grammar used in agent ``tools:`` blocks.
"""

from __future__ import annotations

from omnigent.spec import parse_default_mcp_servers


def test_none_and_empty_yield_no_servers() -> None:
    # Absent key (None) or an empty mapping must be a clean no-op so the
    # server starts with no default MCP servers — the opt-in default.
    assert parse_default_mcp_servers(None) == []
    assert parse_default_mcp_servers({}) == []


def test_parses_http_server_with_headers() -> None:
    servers = parse_default_mcp_servers(
        {
            "company_search": {
                "type": "mcp",
                "url": "https://mcp.company.com/sse",
                "headers": {"Authorization": "Bearer tok"},
            }
        }
    )
    assert len(servers) == 1
    s = servers[0]
    assert s.name == "company_search"
    assert s.transport == "http"
    assert s.url == "https://mcp.company.com/sse"
    assert s.headers == {"Authorization": "Bearer tok"}


def test_parses_stdio_server_with_command_and_args() -> None:
    servers = parse_default_mcp_servers(
        {
            "local_fs": {
                "type": "mcp",
                "command": "npx",
                "args": ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"],
            }
        }
    )
    assert len(servers) == 1
    s = servers[0]
    assert s.name == "local_fs"
    assert s.transport == "stdio"
    assert s.command == "npx"
    assert s.args == ["-y", "@modelcontextprotocol/server-filesystem", "/tmp"]


def test_preserves_yaml_key_order() -> None:
    servers = parse_default_mcp_servers(
        {
            "alpha": {"type": "mcp", "url": "https://a.example/sse"},
            "bravo": {"type": "mcp", "url": "https://b.example/sse"},
        }
    )
    assert [s.name for s in servers] == ["alpha", "bravo"]
