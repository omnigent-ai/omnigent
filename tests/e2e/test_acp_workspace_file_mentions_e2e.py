"""E2E test for per-host ACP ``workspace_file_mentions`` advertisement.

Spawns an isolated host daemon with ACP agents configured in its local
``config.yaml`` against a separate ``live_server`` (which has no ACP
config of its own), then verifies ``workspace_file_mentions`` reaches
the REST API on initial connect and updates on explicit harness refresh.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import yaml

from tests._helpers.host import isolated_host


def test_host_advertises_and_updates_acp_workspace_file_mentions(
    tmp_path: Path,
    live_server: str,
    http_client: httpx.Client,
    mock_llm_server_url: str | None,
) -> None:
    """Host-configured ``acp:<slug>`` file-mention flags reach ``/v1/hosts`` across connects."""
    stub_bin = tmp_path / "stub-acp"
    stub_bin.write_text("#!/bin/sh\nexit 0\n")
    stub_bin.chmod(0o755)

    host_id = uuid.uuid4().hex
    host_name = f"e2e-host-{host_id[:12]}"
    env = (
        {"OPENAI_BASE_URL": f"{mock_llm_server_url}/v1", "OPENAI_API_KEY": "mock-key"}
        if mock_llm_server_url
        else None
    )

    initial_config = {
        "acp": {
            "agents": [
                {
                    "name": "Mentions ACP",
                    "command": str(stub_bin),
                    "workspace_file_mentions": True,
                },
                {
                    "name": "Plain ACP",
                    "command": str(stub_bin),
                },
            ]
        }
    }
    expected_initial = {"acp:mentions-acp": True, "acp:plain-acp": False}

    with isolated_host(
        tmp_path / "initial",
        live_server,
        env=env,
        config=initial_config,
        host_id=host_id,
        name=host_name,
        ready=lambda host: host.wait_online(http_client),
    ) as host:
        list_resp = http_client.get("/v1/hosts")
        assert list_resp.status_code == 200, list_resp.text
        list_row = next(h for h in list_resp.json()["hosts"] if h["host_id"] == host.host_id)
        assert list_row["workspace_file_mentions"] == expected_initial

        detail_resp = http_client.get(f"/v1/hosts/{host.host_id}")
        assert detail_resp.status_code == 200, detail_resp.text
        assert detail_resp.json()["workspace_file_mentions"] == expected_initial

    # Flip the flags on the host and reconnect with the same identity.
    initial_cfg_path = tmp_path / "initial" / "home" / ".omnigent" / "config.yaml"
    updated_config = yaml.safe_load(initial_cfg_path.read_text())
    updated_config.pop("host", None)
    for agent in updated_config["acp"]["agents"]:
        agent["workspace_file_mentions"] = agent["name"] == "Plain ACP"
    expected_updated = {"acp:mentions-acp": False, "acp:plain-acp": True}

    with isolated_host(
        tmp_path / "reconnected",
        live_server,
        env=env,
        config=updated_config,
        host_id=host_id,
        name=host_name,
        ready=lambda host: host.wait_online(http_client),
    ) as host:
        after_resp = http_client.get(f"/v1/hosts/{host.host_id}")
        assert after_resp.status_code == 200, after_resp.text
        assert after_resp.json()["workspace_file_mentions"] == expected_updated
