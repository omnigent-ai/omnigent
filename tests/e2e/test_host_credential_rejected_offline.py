"""A host whose credential the workspace edge starts rejecting goes offline, then recovers."""

from __future__ import annotations

import json
import os
import re
import shutil
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import yaml

from tests._helpers.compat import runner_executable
from tests._helpers.host import Host, isolated_host
from tests._helpers.live_server import isolated_local_server
from tests._helpers.polling import poll_until
from tests.e2e._host_login_workspace import (
    ACCESS_TOKEN,
    USER_EMAIL,
    WORKSPACE_ID,
    HostLoginWorkspace,
)

pytestmark = [
    pytest.mark.skipif(not shutil.which("openssl"), reason="openssl required for the test CA"),
    pytest.mark.timeout(240),
    pytest.mark.min_runner_version("0.21.0"),
]

_LAUNCH_LINE = re.compile(r"Launched runner (\S+) for workspace")
_USER_HEADERS = {"X-Forwarded-Email": USER_EMAIL}


def _write_profile(path: Path, workspace_url: str, token: str) -> None:
    path.write_text(f"[local-test]\nhost = {workspace_url}\nauth_type = pat\ntoken = {token}\n")


@contextmanager
def _edge_host(root: Path, edge: HostLoginWorkspace, host_id: str) -> Iterator[Host]:
    """Run ``omnigent host`` against the edge with a PAT profile and a stored workspace login."""
    home = root / "home"
    data = home / ".omnigent"
    data.mkdir(parents=True)
    (data / "auth_tokens.json").write_text(
        json.dumps(
            {
                edge.server_url: {
                    "auth_type": "databricks",
                    "workspace_host": edge.url,
                    "org_id": WORKSPACE_ID,
                    "user_id": USER_EMAIL,
                }
            }
        )
    )
    _write_profile(home / ".databrickscfg", edge.url, ACCESS_TOKEN)
    # Loopback clients ignore SSL_CERT_FILE, so make the test CA certifi's bundle
    # for the host and the runners it launches.
    shim = root / "ca-shim"
    shim.mkdir()
    (shim / "sitecustomize.py").write_text(
        "import os, certifi\ncertifi.where = lambda: os.environ['SSL_CERT_FILE']\n"
    )
    with isolated_host(
        root,
        edge.server_url,
        env={
            "OMNIGENT_NO_UPDATE_CHECK": "1",
            "DATABRICKS_CONFIG_FILE": str(home / ".databrickscfg"),
            "DATABRICKS_CONFIG_PROFILE": "local-test",
            "SSL_CERT_FILE": str(edge.cert),
            "REQUESTS_CA_BUNDLE": str(edge.cert),
            "PYTHONPATH": os.pathsep.join(
                filter(None, [str(shim), os.environ.get("PYTHONPATH", "")])
            ),
        },
        host_id=host_id,
        name=f"edge-host-{host_id[:8]}",
        command=[runner_executable(), "-m", "omnigent", "host", edge.server_url, "--no-open"],
    ) as host:
        yield host


def _host_status(client: httpx.Client, host_id: str) -> str | None:
    hosts = client.get("/v1/hosts").json()["hosts"]
    return next((host["status"] for host in hosts if host["host_id"] == host_id), None)


def _runner_online(client: httpx.Client, runner_id: str) -> bool:
    response = client.get(f"/v1/runners/{runner_id}/status")
    return response.status_code == 200 and response.json().get("online") is True


def _rejected_host_tunnels(edge: HostLoginWorkspace, host_id: str) -> int:
    tunnel = f"/v1/hosts/{host_id}/tunnel"
    return sum(
        1
        for entry in list(edge.edge_log)
        if entry["status"] == 401 and tunnel in str(entry["path"])
    )


def _launch_runner(
    client: httpx.Client, host_log: Path, agent_id: str, host_id: str, workspace: Path
) -> tuple[str, str]:
    """Create a session bound to the host and return it with the runner it launches."""

    def launched() -> list[str]:
        return _LAUNCH_LINE.findall(host_log.read_text(errors="replace"))

    before = len(launched())
    created = client.post(
        "/v1/sessions",
        json={"agent_id": agent_id, "host_id": host_id, "workspace": str(workspace)},
        timeout=90.0,
    )
    created.raise_for_status()
    poll_until(lambda: len(launched()) > before, timeout_s=60, interval_s=0.25)
    return created.json()["id"], launched()[before]


def test_host_goes_offline_when_its_credential_is_rejected_and_recovers(tmp_path: Path) -> None:
    agent_name = f"edge-host-agent-{uuid.uuid4().hex[:6]}"
    agent_yaml = tmp_path / "agent.yaml"
    agent_yaml.write_text(
        yaml.safe_dump(
            {
                "name": agent_name,
                "prompt": "You are a terse test assistant.",
                "executor": {
                    "harness": "openai-agents",
                    "model": "mock-model",
                    "auth": {"type": "api_key", "api_key": "mock-key"},
                },
            }
        )
    )
    workspace = tmp_path / "project"
    workspace.mkdir()
    host_id = uuid.uuid4().hex
    with (
        isolated_local_server(
            tmp_path / "server", extra_args=["--agent", str(agent_yaml)]
        ) as base_url,
        HostLoginWorkspace(tmp_path / "workspace", base_url) as edge,
        httpx.Client(base_url=base_url, headers=_USER_HEADERS, timeout=15.0) as client,
        _edge_host(tmp_path / "host", edge, host_id) as host,
    ):
        poll_until(lambda: _host_status(client, host_id) == "online", timeout_s=60)
        agents = client.get("/v1/agents").json()["data"]
        agent_id = next(agent["id"] for agent in agents if agent["name"] == agent_name)
        _, healthy = _launch_runner(client, host.log_path, agent_id, host_id, workspace)
        poll_until(lambda: _runner_online(client, healthy), timeout_s=60)

        edge.revoke_issued_tokens()
        session_id, rejected = _launch_runner(client, host.log_path, agent_id, host_id, workspace)
        # A send waiting on the refused runner still learns the cause.
        send = client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {"role": "user", "content": [{"type": "input_text", "text": "hi"}]},
            },
            timeout=120.0,
        )
        assert send.status_code == 503, send.text
        assert "rejected this host's credentials" in send.text
        assert " login " in send.text
        poll_until(lambda: _host_status(client, host_id) == "offline", timeout_s=60)
        assert not _runner_online(client, rejected)
        assert _runner_online(client, healthy)
        assert "now rejects this host's credentials" in host.log_path.read_text(errors="replace")
        poll_until(lambda: _rejected_host_tunnels(edge, host_id) > 0)
        assert _host_status(client, host_id) == "offline"
        assert host.proc.poll() is None

        _write_profile(host.home / ".databrickscfg", edge.url, edge.issue_token())
        poll_until(lambda: _host_status(client, host_id) == "online", timeout_s=60)
        _, recovered = _launch_runner(client, host.log_path, agent_id, host_id, workspace)
        poll_until(lambda: _runner_online(client, recovered), timeout_s=60)
        assert host.proc.poll() is None
