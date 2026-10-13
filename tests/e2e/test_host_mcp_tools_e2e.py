"""MCP tool names travels from a real host daemon through the REST API."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

from tests._helpers.host import isolated_host


@pytest.mark.min_server_version("0.17.0")
def test_host_mcp_tools(live_server: str, tmp_path: Path) -> None:
    script = tmp_path / "mcp_server.py"
    script.write_text(
        "import os\n"
        "from mcp.server.fastmcp import FastMCP\n"
        "mcp = FastMCP('fixture')\n"
        "@mcp.tool(name=os.environ.get('TOOL_NAME', 'read_docs'))\n"
        "def read_docs() -> str:\n"
        '    "Read documentation."\n'
        "    return 'never called'\n"
        "mcp.run()\n"
    )
    claude = tmp_path / "claude"
    claude.mkdir()
    (claude / ".claude.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "docs": {
                        "command": sys.executable,
                        "args": [str(script)],
                        "env": {"TOKEN": "synthetic-config-secret"},
                    }
                }
            }
        )
    )
    installs = {}
    for market in ("first", "second"):
        plugin = claude / "plugins" / "cache" / market
        plugin.mkdir(parents=True)
        installs[f"toolkit@{market}"] = [{"installPath": str(plugin)}]
        (plugin / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "docs": {
                            "command": sys.executable,
                            "args": [str(script)],
                            "env": {"TOOL_NAME": market},
                        }
                    }
                }
            )
        )
    (claude / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": installs}))
    (claude / "settings.json").write_text(
        json.dumps({"enabledPlugins": dict.fromkeys(installs, True)})
    )
    with (
        httpx.Client(base_url=live_server, timeout=20) as client,
        isolated_host(
            tmp_path,
            live_server,
            env={
                "USERPROFILE": str(tmp_path / "home"),
                "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
            },
        ) as host,
    ):
        host.wait_online(client, timeout=45, poll_interval=0.1)
        host_id = host.host_id
        response = client.post(
            f"/v1/hosts/{host_id}/mcp-servers/tools",
            json={"harness": "claude", "server": "docs"},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "tools": [{"name": "read_docs", "description": "Read documentation."}],
            "connection": "connected",
            "truncated": False,
        }
        assert "synthetic-config-secret" not in response.text
        assert str(tmp_path) not in response.text
        plugins = client.get(f"/v1/hosts/{host_id}/plugins").json()["plugins"]
        active = client.get(f"/v1/hosts/{host_id}/mcp-servers").json()["mcp_servers"]
        active_ids = {server["source_id"] for server in active if server["plugin"]}
        for plugin in plugins:
            asset = plugin["mcp_entries"][0]
            assert asset["id"] in active_ids
            response = client.post(
                f"/v1/hosts/{host_id}/mcp-servers/tools",
                json={
                    "harness": "claude",
                    "server": "docs",
                    "plugin": "toolkit",
                    "source_id": asset["id"],
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["tools"][0]["name"] == plugin["marketplace"]
    assert "synthetic-config-secret" not in host.log_path.read_text()
