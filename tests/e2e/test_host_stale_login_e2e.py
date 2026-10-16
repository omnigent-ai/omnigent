"""A host whose stored login lapsed must not keep reporting online while its runners are rejected.

Stand-in for a server behind an authenticating front door (the Databricks Apps
edge): an accounts-mode ``omnigent server`` sits behind
:class:`~tests.e2e._auth_edge_proxy.AuthEdgeProxy`, which answers 401 to any
``/v1/*`` request or WebSocket upgrade whose credential the server no longer
accepts. The real host daemon connects through the edge with a stored login whose
session token has a short lifetime. Its tunnel outlives that token, and every
runner it launches afterwards presents no accepted credential: the runner is
rejected on ``POST /v1/runners/{id}/token`` and on three tunnel upgrades, then
exits, while the host keeps reporting itself online and keeps accepting launches.

Run with::

    python -m pytest tests/e2e/test_host_stale_login_e2e.py -v --timeout=900
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import IO

import httpx
import jwt
import pytest
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from omnigent.server.oidc import mint_session_token
from tests._helpers.compat import (
    apply_runner_env,
    apply_server_env,
    compat_runner_cwd,
    compat_server_cwd,
    runner_executable,
    server_executable,
)
from tests._helpers.live_server import find_free_port
from tests.e2e._auth_edge_proxy import AuthEdgeProxy
from tests.e2e.helpers import POLL_INTERVAL_S

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Shared with the server subprocess so the test can mint a session token in the
# exact shape ``omnigent login`` stores (and the server validates).
_COOKIE_SECRET_HEX = "5e1f0c9a3b7d4e2f8a6c1d0b9e7f3a5c2d4b6e8f0a1c3e5d7b9f1a3c5e7d9b1f"
_ADMIN = "admin"
_ADMIN_PASSWORD = "stale-login-e2e-pw-123456"
_AGENT_NAME = "hello_world"
_AGENT_YAML = """\
name: hello_world
prompt: You are a friendly assistant. Say hello and answer questions.

executor:
  model: gpt-4o-mini
  harness: openai-agents
"""

_SERVER_HEALTH_TIMEOUT_S = 90.0
_HOST_ONLINE_TIMEOUT_S = 120.0
_RUNNER_TIMEOUT_S = 60.0
# Short stand-in for a multi-hour login lifetime that lapses mid-session; long
# enough for the baseline runner to settle before the token expires.
_LOGIN_TTL_S = 90.0


@dataclass
class StaleLoginRig:
    """An accounts-mode server behind the auth edge plus one host daemon's isolated state."""

    edge: AuthEdgeProxy
    server_url: str
    mock_llm_url: str
    host_id: str
    host_name: str
    home: Path
    data_dir: Path
    config_home: Path
    workspace: Path
    host_log: Path
    host_stderr: Path
    server_log: Path
    admin_token: str
    account_generation: str | None
    server_proc: subprocess.Popen[bytes]
    host_proc: subprocess.Popen[bytes] | None = None
    host_stderr_fh: IO[str] | None = None
    server_log_fh: IO[str] | None = None
    login_expires_at: float = 0.0

    @property
    def url(self) -> str:
        """Server URL as the host, runners and browser see it (the edge)."""
        return self.edge.url

    def client(self) -> httpx.Client:
        return httpx.Client(
            base_url=self.url,
            headers={"Authorization": f"Bearer {self.admin_token}"},
            timeout=30.0,
            trust_env=False,
        )

    def store_host_login(self, ttl_s: float) -> float:
        """Store a stored-login record for the edge URL the way ``omnigent login`` does."""
        now = time.time()
        token = mint_session_token(
            _ADMIN,
            bytes.fromhex(_COOKIE_SECRET_HEX),
            int(ttl_s),
            "accounts",
            account_generation=self.account_generation,
        )
        expires_at = now + ttl_s
        subprocess.run(
            [
                sys.executable,
                "-c",
                "import json, sys; from omnigent import cli_auth; "
                "cli_auth.store_token(**json.loads(sys.argv[1]))",
                json.dumps(
                    {
                        "server_url": self.url,
                        "token": token,
                        "user_id": _ADMIN,
                        "expires_at": expires_at,
                    }
                ),
            ],
            env=self._host_env(),
            check=True,
            timeout=60,
        )
        self.login_expires_at = expires_at
        return expires_at

    def _host_env(self) -> dict[str, str]:
        env = {
            key: value
            for key, value in os.environ.items()
            # Exclude DATABRICKS_* so the runner cannot fall back to SDK OAuth
            # and mask the expired stored login this test depends on.
            if not key.startswith(
                (
                    "OMNIGENT_RUNNER",
                    "OMNIGENT_PROCESS",
                    "OMNIGENT_HOST",
                    "OPENAI_",
                    "ANTHROPIC_",
                    "DATABRICKS_",
                )
            )
        }
        for key in (
            "RUNNER_SERVER_URL",
            "OMNIGENT_LOCAL_SINGLE_USER",
            "OMNIGENT_DATA_DIR",
            "OMNIGENT_CONFIG_HOME",
            "CLAUDECODE",
        ):
            env.pop(key, None)
        env.update(
            {
                "HOME": str(self.home),
                "OMNIGENT_DATA_DIR": str(self.data_dir),
                "OMNIGENT_CONFIG_HOME": str(self.config_home),
                "OPENAI_BASE_URL": f"{self.mock_llm_url}/v1",
                "OPENAI_API_KEY": "mock-key",
                PROCESS_LOG_FILE_ENV_VAR: str(self.host_log),
                "PYTHONPATH": str(_REPO_ROOT),
            }
        )
        return apply_runner_env(env)

    def start_host(self) -> None:
        """Run the real host daemon (`omnigent host --server <edge>`) against the edge."""
        stderr = open(self.host_stderr, "a")  # noqa: SIM115 — closed in stop_host
        self.host_stderr_fh = stderr
        self.host_proc = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", self.url],
            env=self._host_env(),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=stderr,
        )

    def host_alive(self) -> bool:
        return self.host_proc is not None and self.host_proc.poll() is None

    def stop_host(self) -> None:
        _terminate(self.host_proc)
        self.host_proc = None
        if self.host_stderr_fh is not None:
            self.host_stderr_fh.close()
            self.host_stderr_fh = None

    def shutdown(self) -> None:
        self.stop_host()
        _terminate(self.server_proc)
        if self.server_log_fh is not None:
            self.server_log_fh.close()
            self.server_log_fh = None
        self.edge.stop()

    def host_record(self, client: httpx.Client) -> dict | None:
        resp = client.get("/v1/hosts")
        resp.raise_for_status()
        for host in resp.json().get("hosts", []):
            if host["host_id"] == self.host_id:
                return host
        return None

    def wait_host_online(
        self, client: httpx.Client, timeout: float = _HOST_ONLINE_TIMEOUT_S
    ) -> dict:
        deadline = time.monotonic() + timeout
        last: dict | None = None
        while time.monotonic() < deadline:
            if not self.host_alive():
                raise AssertionError(f"host daemon exited early:\n{self.host_log_tail()}")
            last = self.host_record(client)
            if last is not None and last["status"] == "online":
                return last
            time.sleep(POLL_INTERVAL_S)
        raise AssertionError(
            f"host {self.host_id} not online within {timeout}s (last={last}):\n"
            f"{self.host_log_tail()}"
        )

    def agent_id(self, client: httpx.Client) -> str:
        resp = client.get("/v1/agents", params={"limit": 100})
        resp.raise_for_status()
        for agent in resp.json()["data"]:
            if agent["name"] == _AGENT_NAME:
                return str(agent["id"])
        raise AssertionError(f"{_AGENT_NAME!r} not registered: {resp.text}")

    def launch_session(self, client: httpx.Client) -> tuple[str, httpx.Response]:
        """Create a session and ask the server to launch its runner on this host."""
        create = client.post("/v1/sessions", json={"agent_id": self.agent_id(client)})
        create.raise_for_status()
        session_id = create.json()["id"]
        launch = client.post(
            f"/v1/hosts/{self.host_id}/runners",
            json={"session_id": session_id, "workspace": str(self.workspace)},
            timeout=60.0,
        )
        return session_id, launch

    def runner_status(self, client: httpx.Client, runner_id: str) -> dict:
        resp = client.get(f"/v1/runners/{runner_id}/status")
        resp.raise_for_status()
        return resp.json()

    def wait_runner_settled(
        self, client: httpx.Client, runner_id: str, timeout: float = _RUNNER_TIMEOUT_S
    ) -> dict:
        """Poll until the runner is online or the host reported its death."""
        deadline = time.monotonic() + timeout
        status: dict = {}
        while time.monotonic() < deadline:
            status = self.runner_status(client, runner_id)
            if status.get("online") is True or status.get("error"):
                return status
            time.sleep(POLL_INTERVAL_S)
        return status

    def host_log_tail(self, limit: int = 4000) -> str:
        parts = []
        for path in (self.host_log, self.host_stderr):
            if path.exists():
                parts.append(f"--- {path.name} ---\n{path.read_text(errors='replace')[-limit:]}")
        return "\n".join(parts)

    def edge_summary(self) -> str:
        return "\n".join(
            f"{r.method} {r.path} -> {r.status} (authorized={r.authorized})"
            for r in self.edge.requests
            if r.path.startswith("/v1/runners/") or r.path.startswith("/v1/hosts/")
        )


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _await_health(base_url: str, proc: subprocess.Popen[bytes], log_path: Path) -> None:
    deadline = time.monotonic() + _SERVER_HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"server exited early:\n{log_path.read_text(errors='replace')[-3000:]}"
            )
        try:
            if httpx.get(f"{base_url}/health", timeout=2, trust_env=False).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise AssertionError(f"server not healthy:\n{log_path.read_text(errors='replace')[-3000:]}")


def _sleep_until(wall_clock: float) -> None:
    remaining = wall_clock - time.time()
    if remaining > 0:
        time.sleep(remaining)


def boot_rig(root: Path, mock_llm_url: str) -> StaleLoginRig:
    """Start the accounts server and the edge; prepare (but do not start) the host."""
    root.mkdir(parents=True, exist_ok=True)
    server_port = find_free_port()
    server_url = f"http://127.0.0.1:{server_port}"
    edge = AuthEdgeProxy(server_url)

    agent_yaml = root / "hello_world.yaml"
    agent_yaml.write_text(_AGENT_YAML)
    server_log = root / "server.log"
    server_env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OMNIGENT_RUNNER", "OMNIGENT_PROCESS", "OMNIGENT_HOST"))
    }
    server_env.pop("RUNNER_SERVER_URL", None)
    server_env.pop("OMNIGENT_OIDC_ISSUER", None)
    server_env.update(
        {
            "OMNIGENT_AUTH_PROVIDER": "accounts",
            "OMNIGENT_AUTH_ENABLED": "1",
            "OMNIGENT_LOCAL_SINGLE_USER": "",
            "OMNIGENT_ACCOUNTS_COOKIE_SECRET": _COOKIE_SECRET_HEX,
            "OMNIGENT_ACCOUNTS_BASE_URL": edge.url,
            "OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME": _ADMIN,
            "OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD": _ADMIN_PASSWORD,
            "OMNIGENT_ACCOUNTS_AUTO_OPEN": "0",
            "OMNIGENT_ADMIN_CREDENTIALS_PATH": str(root / "admin-credentials"),
            "OMNIGENT_CONFIG_HOME": str(root / "server-config"),
            "OMNIGENT_DATA_DIR": str(root / "server-data"),
            "OPENAI_BASE_URL": f"{mock_llm_url}/v1",
            "OPENAI_API_KEY": "mock-key",
            "ANTHROPIC_API_KEY": "",
        }
    )
    apply_server_env(server_env, _REPO_ROOT)
    server_handle = open(server_log, "w")  # noqa: SIM115 — lives for the Popen
    server_proc = subprocess.Popen(
        [
            server_executable(),
            "-m",
            "omnigent.cli",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(server_port),
            "--database-uri",
            f"sqlite:///{root / 'server.db'}",
            "--artifact-location",
            str(root / "artifacts"),
            "--agent",
            str(agent_yaml),
        ],
        env=server_env,
        cwd=compat_server_cwd(),
        stdout=server_handle,
        stderr=subprocess.STDOUT,
    )
    try:
        _await_health(server_url, server_proc, server_log)
        edge.start()
        assert httpx.get(f"{edge.url}/health", timeout=5, trust_env=False).status_code == 200

        login = httpx.post(
            f"{edge.url}/auth/login",
            json={"username": _ADMIN, "password": _ADMIN_PASSWORD},
            timeout=30,
            trust_env=False,
        )
        assert login.status_code == 200, login.text
        admin_token = login.json()["token"]
        claims = jwt.decode(admin_token, bytes.fromhex(_COOKIE_SECRET_HEX), algorithms=["HS256"])
    except BaseException:
        _terminate(server_proc)
        edge.stop()
        raise

    host_id = uuid.uuid4().hex
    host_name = f"stale-login-host-{host_id[:8]}"
    config_home = root / "host-config"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        yaml.safe_dump({"host": {"host_id": host_id, "name": host_name}}, sort_keys=True)
    )
    home = root / "host-home"
    home.mkdir()
    workspace = root / "workspace"
    workspace.mkdir()
    return StaleLoginRig(
        edge=edge,
        server_url=server_url,
        mock_llm_url=mock_llm_url,
        host_id=host_id,
        host_name=host_name,
        home=home,
        data_dir=root / "host-data",
        config_home=config_home,
        workspace=workspace,
        host_log=root / "host-daemon.log",
        host_stderr=root / "host-stderr.log",
        server_log=server_log,
        server_log_fh=server_handle,
        admin_token=admin_token,
        account_generation=claims.get("account_generation"),
        server_proc=server_proc,
    )


@pytest.fixture
def stale_login_rig(tmp_path: Path, mock_llm_server_url: str) -> Iterator[StaleLoginRig]:
    rig = boot_rig(tmp_path / "rig", mock_llm_server_url)
    try:
        yield rig
    finally:
        rig.shutdown()


@pytest.mark.timeout(900)
def test_host_stops_reporting_online_once_runner_login_is_rejected(
    stale_login_rig: StaleLoginRig,
) -> None:
    """After its login lapses, a host whose runners are rejected must not stay plainly online."""
    rig = stale_login_rig
    expires_at = rig.store_host_login(ttl_s=_LOGIN_TTL_S)
    rig.start_host()
    with rig.client() as client:
        rig.wait_host_online(client)
        _, baseline = rig.launch_session(client)
        assert baseline.status_code == 200, baseline.text
        baseline_status = rig.wait_runner_settled(client, baseline.json()["runner_id"])
        assert baseline_status.get("online") is True, (
            f"baseline runner never connected: {baseline_status}\n{rig.host_log_tail()}"
        )
        # A live login connects cleanly: no tunnel upgrade was rejected yet.
        assert not rig.edge.rejections("/tunnel"), rig.edge_summary()
        assert time.time() < expires_at, (
            "login expired before the baseline runner settled; raise _LOGIN_TTL_S"
        )

        _sleep_until(expires_at + 3.0)
        assert rig.host_alive(), rig.host_log_tail()
        still_connected = rig.host_record(client)
        assert still_connected is not None and still_connected["status"] == "online", (
            f"host tunnel did not outlive its login: {still_connected}"
        )

        _, launch = rig.launch_session(client)
        # Once the stored login lapsed the server refuses the launch up front
        # with an actionable re-login error instead of accepting a runner that
        # would be rejected (HTTP 401) on its token endpoint and exit.
        assert launch.status_code == 503, launch.text
        error = launch.json().get("error", {})
        assert error.get("code") == "host_login_expired", launch.text
        assert "login" in (error.get("message") or "").lower(), launch.text
        # No runner was spawned, so the edge saw no runner-token rejection.
        assert not rig.edge.rejections("/token"), rig.edge_summary()
