"""Skill contents travels from a real host daemon through the REST API."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from tests._helpers.host import isolated_host


@pytest.mark.min_server_version("0.17.0")
def test_host_skill_content(live_server: str, tmp_path: Path) -> None:
    skill = tmp_path / "claude" / "skills" / "review"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: review\ndescription: Review diffs.\n---\n# Synthetic private instructions"
    )
    (skill / "other.md").write_text("synthetic-other-file-secret")
    plugin = tmp_path / "claude" / "plugins" / "cache" / "toolkit"
    bundled = plugin / "skills" / "lint"
    bundled.mkdir(parents=True)
    (bundled / "SKILL.md").write_text(
        "---\nname: lint\ndescription: Installed\nuser-invocable: false\n---\nPlugin private body"
    )
    (tmp_path / "claude" / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"toolkit@test": [{"installPath": str(plugin)}]}})
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
        listing = client.get(f"/v1/skills?host_id={host_id}&harness=claude-native&path=~")
        assert listing.status_code == 200
        assert "Synthetic private instructions" not in listing.text
        response = client.get(f"/v1/hosts/{host_id}/harnesses/claude-native/skills/review")
        assert response.status_code == 200, response.text
        assert response.json() == {
            "name": "review",
            "description": "Review diffs.",
            "content": "# Synthetic private instructions",
            "truncated": False,
        }
        assert response.headers["cache-control"] == "no-store"
        assert "synthetic-other-file-secret" not in response.text
        assert str(tmp_path) not in response.text
        plugins = client.get(f"/v1/hosts/{host_id}/plugins").json()["plugins"]
        assert plugins[0]["enabled"] is False
        asset = plugins[0]["skill_entries"][0]
        response = client.get(
            f"/v1/hosts/{host_id}/harnesses/claude-native/skills/toolkit:lint",
            params={"source_id": asset["id"]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["content"] == "Plugin private body"
        assert response.headers["cache-control"] == "no-store"
        assert str(tmp_path) not in response.text
    assert "Synthetic private instructions" not in host.log_path.read_text()
    assert "Plugin private body" not in host.log_path.read_text()
