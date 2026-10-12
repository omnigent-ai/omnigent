"""Installed plugin metadata travels from a real host daemon through the REST API."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from omnigent.spec.skill_sources import _plugin_asset_id
from tests._helpers.host import isolated_host


@pytest.mark.min_server_version("0.17.0")
def test_host_plugin_inventory(live_server: str, tmp_path: Path) -> None:
    root = tmp_path / "claude" / "plugins"
    plugin = root / "cache" / "hooks"
    (plugin / ".claude-plugin").mkdir(parents=True)
    (plugin / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(
            {
                "version": "1.2.3",
                "description": "Test hook plugin",
                "hooks": {"command": "synthetic-private-command"},
            }
        )
    )
    (root / "installed_plugins.json").write_text(
        json.dumps(
            {
                "plugins": {"hooks@test-market": [{"installPath": str(plugin)}]},
            }
        )
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
        response = client.get(f"/v1/hosts/{host_id}/plugins")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "plugins": [
                {
                    "id": _plugin_asset_id("hooks@test-market", "plugin"),
                    "skill_entries": [],
                    "mcp_entries": [],
                    "harness": "claude",
                    "name": "hooks",
                    "marketplace": "test-market",
                    "version": "1.2.3",
                    "description": "Test hook plugin",
                    "enabled": False,
                    "skills": [],
                    "mcp_servers": [],
                    "has_hooks": True,
                    "has_commands": False,
                }
            ]
        }
        assert "synthetic-private-command" not in response.text
        assert str(tmp_path) not in response.text
    assert "synthetic-private-command" not in host.log_path.read_text()
