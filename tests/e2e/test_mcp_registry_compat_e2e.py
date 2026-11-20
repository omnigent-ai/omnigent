"""Native inline registry references survive servers that ignore the new transport."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.spec import load
from tests._helpers.compat import meets_min_server_version


def _registry_bundle() -> bytes:
    """Build root and child references with distinct authored tool restrictions."""
    executor = {
        "type": "omnigent",
        "model": "registry-compat-test",
        "config": {"harness": "claude-sdk"},
    }
    configs = {
        "config.yaml": {
            "spec_version": 1,
            "name": "registry-compat",
            "executor": executor,
            "tools": {
                "agents": ["worker"],
                "ordinary": {
                    "type": "mcp",
                    "transport": "http",
                    "url": "https://example.test/ordinary",
                    "tools": ["read_existing"],
                },
                "tracker": {"type": "mcp", "transport": "registry", "tools": []},
            },
        },
        "agents/worker/config.yaml": {
            "spec_version": 1,
            "name": "worker",
            "executor": executor,
            "tools": {
                "tracker": {
                    "type": "mcp",
                    "transport": "registry",
                    "tools": ["read_ticket"],
                }
            },
        },
    }
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, config in configs.items():
            data = yaml.safe_dump(config).encode()
            member = tarfile.TarInfo(name)
            member.size = len(data)
            archive.addfile(member, io.BytesIO(data))
    return output.getvalue()


@pytest.mark.compat_smoke
def test_native_inline_registry_bundle_survives_old_servers(
    live_server: str, server_version: str, tmp_path: Path
) -> None:
    """Upload and download through real HTTP without executing a turn or an MCP."""
    bundle = _registry_bundle()
    # Servers before 0.19.0 drop the registry transport and the selection header.
    old_server = not meets_min_server_version(server_version, "0.19.0")
    with httpx.Client(
        base_url=live_server,
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        timeout=30,
    ) as client:
        created = client.post(
            "/v1/sessions",
            data={"metadata": "{}"},
            files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        )
        assert created.status_code == 201, created.text
        session_id = created.json()["session_id"]
        agent_url = f"/v1/sessions/{session_id}/agent"
        summary = client.get(agent_url)
        assert summary.status_code == 200, summary.text
        servers = {server["name"]: server for server in summary.json()["mcp_servers"]}
        assert set(servers) == ({"ordinary"} if old_server else {"ordinary", "tracker"})
        assert servers["ordinary"]["transport"] == "http"
        assert servers["ordinary"]["url"] == "https://example.test/ordinary"
        if not old_server:
            assert servers["tracker"]["transport"] == "registry"

        for negotiated in (False, True):
            response = client.get(
                f"{agent_url}/contents",
                headers={"X-Omnigent-MCP-Selection": "1"} if negotiated else {},
            )
            assert response.status_code == 200, response.text
            assert response.content == bundle
            if negotiated and not old_server:
                assert json.loads(response.headers["X-Omnigent-MCP-Selection"]) == {}
            else:
                assert "X-Omnigent-MCP-Selection" not in response.headers

            # The current reader recovers restrictions even after an old server stores them.
            spec = load(response.content, dest=tmp_path / str(negotiated), expand_env=False)
            root_servers = {server.name: server for server in spec.mcp_servers}
            assert root_servers["tracker"].transport == "registry"
            assert root_servers["tracker"].tools == []
            assert root_servers["ordinary"].tools == ["read_existing"]
            assert [child.name for child in spec.sub_agents] == ["worker"]
            child_server = spec.sub_agents[0].mcp_servers[0]
            assert child_server.name == "tracker"
            assert child_server.transport == "registry"
            assert child_server.tools == ["read_ticket"]
