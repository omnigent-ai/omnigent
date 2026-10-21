"""A host whose stored login lapsed must refuse launches its runners could not authenticate.

An accounts-mode server sits behind :class:`~tests.e2e._auth_edge_proxy.AuthEdgeProxy`,
a stand-in for an authenticating front door such as the Databricks Apps edge.
The real host daemon connects through the edge with a short-lived stored login,
and its control tunnel outlives that login. A runner launched afterwards would be
rejected (HTTP 401) on its token endpoint and tunnel upgrades, so the launch must
be refused up front with a re-login hint while the host stays connected. Without
the edge, the runner mints its own owner token, so the same launch must proceed.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest

from omnigent.server.oidc import mint_session_token
from tests._helpers.host import Host, isolated_host
from tests._helpers.live_server import isolated_local_server, isolated_process_env
from tests.e2e._auth_edge_proxy import AuthEdgeProxy

COOKIE_SECRET_HEX = "5e1f0c9a3b7d4e2f8a6c1d0b9e7f3a5c2d4b6e8f0a1c3e5d7b9f1a3c5e7d9b1f"
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "stale-login-e2e-pw-123456"
AGENT_NAME = "hello_world"
AGENT_YAML = """\
name: hello_world
prompt: You are a friendly assistant. Say hello and answer questions.

executor:
  model: gpt-4o-mini
  harness: openai-agents
"""
RUNNER_SETTLE_TIMEOUT_S = 90.0
# Long enough for the baseline runner to connect before the login lapses.
LOGIN_TTL_S = 90.0


@dataclass
class StaleLoginStack:
    edge: AuthEdgeProxy
    url: str
    mock_llm_url: str
    admin_token: str
    account_generation: str | None
    workspace: Path

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.url,
            headers={"Authorization": f"Bearer {self.admin_token}"},
            timeout=30.0,
            trust_env=False,
        )

    def host_env(self) -> dict[str, str | None]:
        return {
            "OPENAI_BASE_URL": f"{self.mock_llm_url}/v1",
            "OPENAI_API_KEY": "mock-key",
            "OMNIGENT_AUTH_PROVIDER": None,
            "OMNIGENT_LOCAL_SINGLE_USER": None,
        }

    def store_host_login(self, host_root: Path, ttl_s: float) -> float:
        """Write the stored login ``omnigent login`` would leave in the host's data dir."""
        expires_at = time.time() + ttl_s
        token = mint_session_token(
            ADMIN_USERNAME,
            bytes.fromhex(COOKIE_SECRET_HEX),
            int(ttl_s),
            "accounts",
            account_generation=self.account_generation,
        )
        record = {
            "server_url": self.url,
            "token": token,
            "user_id": ADMIN_USERNAME,
            "expires_at": expires_at,
        }
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, sys; from omnigent import cli_auth; "
                "cli_auth.store_token(**json.loads(sys.argv[1]))",
                json.dumps(record),
            ],
            env=isolated_process_env(host_root / "home", {}, None),
            check=True,
            timeout=60,
        )
        return expires_at

    def host_record(self, client: httpx.Client, host_id: str) -> dict[str, Any] | None:
        response = client.get("/v1/hosts")
        response.raise_for_status()
        return next(
            (row for row in response.json().get("hosts", []) if row["host_id"] == host_id),
            None,
        )

    def launch_session(self, client: httpx.Client, host_id: str) -> httpx.Response:
        """Create a session and launch its runner on the host, as New Chat does."""
        agents = client.get("/v1/agents", params={"limit": 100})
        agents.raise_for_status()
        agent_id = next(a["id"] for a in agents.json()["data"] if a["name"] == AGENT_NAME)
        created = client.post("/v1/sessions", json={"agent_id": agent_id})
        created.raise_for_status()
        return client.post(
            f"/v1/hosts/{host_id}/runners",
            json={"session_id": created.json()["id"], "workspace": str(self.workspace)},
            timeout=60.0,
        )

    def wait_runner_settled(self, client: httpx.Client, runner_id: str) -> dict[str, Any]:
        """Poll until the runner connected or its host reported an error."""
        deadline = time.monotonic() + RUNNER_SETTLE_TIMEOUT_S
        status: dict[str, Any] = {}
        while time.monotonic() < deadline:
            response = client.get(f"/v1/runners/{runner_id}/status")
            response.raise_for_status()
            status = response.json()
            if status.get("online") is True or status.get("error"):
                return status
            time.sleep(0.5)
        return status

    def edge_summary(self) -> str:
        return "\n".join(
            f"{r.method} {r.path} -> {r.status}"
            for r in list(self.edge.requests)
            if r.path.startswith(("/v1/runners/", "/v1/hosts/"))
        )


@contextmanager
def stale_login_stack(
    root: Path, mock_llm_url: str, *, via_edge: bool = True
) -> Iterator[StaleLoginStack]:
    """Run an accounts-mode server, behind the auth edge unless *via_edge* is False."""
    root.mkdir(parents=True, exist_ok=True)
    agent_yaml = root / "hello_world.yaml"
    agent_yaml.write_text(AGENT_YAML)
    workspace = root / "workspace"
    workspace.mkdir()
    edge = AuthEdgeProxy()
    server_env = {
        "OMNIGENT_AUTH_PROVIDER": "accounts",
        "OMNIGENT_AUTH_ENABLED": "1",
        "OMNIGENT_LOCAL_SINGLE_USER": None,
        "OMNIGENT_ACCOUNTS_COOKIE_SECRET": COOKIE_SECRET_HEX,
        "OMNIGENT_ACCOUNTS_BASE_URL": edge.url,
        "OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME": ADMIN_USERNAME,
        "OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD": ADMIN_PASSWORD,
        "OMNIGENT_ACCOUNTS_AUTO_OPEN": "0",
        "OMNIGENT_ADMIN_CREDENTIALS_PATH": str(root / "admin-credentials"),
        "OPENAI_BASE_URL": f"{mock_llm_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }
    try:
        with isolated_local_server(
            root / "server", env=server_env, extra_args=["--agent", str(agent_yaml)]
        ) as upstream:
            edge.start(upstream)
            url = edge.url if via_edge else upstream
            login = httpx.post(
                f"{url}/auth/login",
                json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
                timeout=30,
                trust_env=False,
            )
            assert login.status_code == 200, login.text
            admin_token = login.json()["token"]
            claims = jwt.decode(
                admin_token, bytes.fromhex(COOKIE_SECRET_HEX), algorithms=["HS256"]
            )
            yield StaleLoginStack(
                edge=edge,
                url=url,
                mock_llm_url=mock_llm_url,
                admin_token=admin_token,
                account_generation=claims.get("account_generation"),
                workspace=workspace,
            )
    finally:
        edge.stop()


@contextmanager
def host_with_stored_login(
    stack: StaleLoginStack, root: Path, ttl_s: float
) -> Iterator[tuple[Host, float]]:
    """Start ``omnigent host`` against the edge with a stored login lasting *ttl_s*."""
    expires_at = stack.store_host_login(root, ttl_s)
    with isolated_host(root, stack.url, env=stack.host_env()) as host:
        yield host, expires_at


@contextmanager
def host_after_login_lapsed(
    tmp_path: Path, mock_llm_url: str, *, via_edge: bool
) -> Iterator[tuple[StaleLoginStack, Host, httpx.Client]]:
    """Connect a host, launch a healthy baseline, then wait until its stored login lapses."""
    with (
        stale_login_stack(tmp_path / "stack", mock_llm_url, via_edge=via_edge) as stack,
        host_with_stored_login(stack, tmp_path / "host", LOGIN_TTL_S) as (host, expires_at),
        stack.client() as client,
    ):
        host.wait_online(client, timeout=120)
        baseline = stack.launch_session(client, host.host_id)
        assert baseline.status_code == 200, baseline.text
        baseline_status = stack.wait_runner_settled(client, baseline.json()["runner_id"])
        assert baseline_status.get("online") is True, (
            f"baseline runner never connected: {baseline_status}\n{stack.edge_summary()}"
        )
        assert not stack.edge.rejections("/tunnel"), stack.edge_summary()
        assert time.time() < expires_at, "login lapsed before the baseline settled"

        time.sleep(max(0.0, expires_at + 3.0 - time.time()))
        assert host.proc.poll() is None, host.log_path.read_text(errors="replace")[-4000:]
        host_row = stack.host_record(client, host.host_id)
        assert host_row is not None and host_row["status"] == "online", (
            f"host tunnel did not outlive its login: {host_row}"
        )
        yield stack, host, client


@pytest.mark.timeout(600)
def test_host_with_lapsed_login_refuses_launch_while_tunnel_stays_online(
    tmp_path: Path, mock_llm_server_url: str
) -> None:
    with host_after_login_lapsed(tmp_path, mock_llm_server_url, via_edge=True) as (
        stack,
        host,
        client,
    ):
        launch = stack.launch_session(client, host.host_id)
        assert launch.status_code == 503, launch.text
        error = launch.json().get("error", {})
        assert error.get("code") == "host_login_expired", launch.text
        assert "login" in (error.get("message") or "").lower(), launch.text
        # Only the host's own admission probe reached the token endpoint; no runner
        # was spawned to be rejected on it or on its tunnel.
        assert len(stack.edge.rejections("/token")) == 1, stack.edge_summary()
        assert not stack.edge.rejections("/tunnel"), stack.edge_summary()
        host_row = stack.host_record(client, host.host_id)
        assert host_row is not None and host_row["status"] == "online", host_row


@pytest.mark.timeout(600)
def test_host_with_lapsed_login_launches_when_runner_mints_its_own_token(
    tmp_path: Path, mock_llm_server_url: str
) -> None:
    """Reached directly, the server mints the runner's owner token from its binding token."""
    with host_after_login_lapsed(tmp_path, mock_llm_server_url, via_edge=False) as (
        stack,
        host,
        client,
    ):
        launch = stack.launch_session(client, host.host_id)
        assert launch.status_code == 200, launch.text
        status = stack.wait_runner_settled(client, launch.json()["runner_id"])
        assert status.get("online") is True, f"runner never connected: {status}"
