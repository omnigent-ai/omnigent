"""E2E coverage for single-user ↔ multi-user auth-mode toggles.

Verifies that a session created in local single-user mode survives being
switched to accounts mode and back, without the operator having to manually
export/import anything.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

_AGENT_YAML = """\
name: hello_world
prompt: You are a friendly assistant. Say hello and answer questions.

executor:
  model: gpt-4o-mini
  harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""

_ADMIN_USERNAME = "toggleadmin"
_ADMIN_PASSWORD = "toggle-admin-pw-123456"


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_server(base_url: str, timeout: float = 30.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{base_url}/health", timeout=2.0)
            if resp.status_code == 200:
                return
        except httpx.ConnectError:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"Server did not become healthy at {base_url}")


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _spawn_server(
    tmp_path: Path,
    extra_env: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[bytes], str]:
    """Start an ``omnigent server`` subprocess and return (proc, base_url)."""
    repo_root = Path(__file__).parents[2].resolve()
    port = _free_port()

    db_path = tmp_path / "toggle.db"
    artifact_dir = tmp_path / "artifacts"
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "hello_world.yaml").write_text(_AGENT_YAML)
    log_path = tmp_path / "server.log"

    base_env = {**os.environ}
    # Strip any ambient auth-mode variables so the test fully controls mode.
    for var in (
        "OMNIGENT_AUTH_PROVIDER",
        "OMNIGENT_AUTH_ENABLED",
        "OMNIGENT_ACCOUNTS_ENABLED",
        "OMNIGENT_LOCAL_SINGLE_USER",
        "OMNIGENT_ACCOUNTS_COOKIE_SECRET",
        "OMNIGENT_ACCOUNTS_BASE_URL",
        "OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME",
        "OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD",
        "OMNIGENT_ACCOUNTS_AUTO_OPEN",
    ):
        base_env.pop(var, None)

    env = {
        **base_env,
        "PYTHONPATH": f"{repo_root}{os.pathsep}{base_env.get('PYTHONPATH', '')}",
        "OMNIGENT_DATA_DIR": str(tmp_path),
        "OPENAI_API_KEY": "dummy",
        "OMNIGENT_ACCOUNTS_AUTO_OPEN": "0",
        **(extra_env or {}),
    }

    log_handle = open(log_path, "w")  # noqa: SIM115
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
            "--agent",
            str(agent_dir),
        ],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_server(base_url)
    except Exception:
        _terminate(proc)
        raise
    return proc, base_url


def _agent_id(base_url: str) -> str:
    resp = httpx.get(f"{base_url}/v1/agents?limit=100", timeout=10.0)
    resp.raise_for_status()
    for agent in resp.json().get("data", []):
        if agent["name"] == "hello_world":
            return str(agent["id"])
    raise RuntimeError("hello_world agent not found in built-in list")


def _create_session(base_url: str, agent_id: str, title: str = "toggle-test") -> str:
    resp = httpx.post(
        f"{base_url}/v1/sessions",
        json={"agent_id": agent_id, "title": title},
        timeout=10.0,
    )
    resp.raise_for_status()
    return str(resp.json()["id"])


def _list_session_ids(base_url: str, *, cookies: dict[str, str] | None = None) -> set[str]:
    resp = httpx.get(
        f"{base_url}/v1/sessions",
        cookies=cookies,
        timeout=10.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return {str(item["id"]) for item in data.get("data", [])}


def _login(base_url: str, username: str, password: str) -> httpx.Cookies:
    client = httpx.Client()
    resp = client.post(
        f"{base_url}/auth/login",
        json={"username": username, "password": password},
        timeout=10.0,
    )
    resp.raise_for_status()
    return client.cookies


@pytest.mark.timeout(120)
def test_session_survives_auth_mode_toggle(tmp_path: Path) -> None:
    """A local session survives switching to accounts and back."""
    proc: subprocess.Popen[bytes] | None = None

    def stop() -> None:
        nonlocal proc
        if proc is not None:
            _terminate(proc)
            proc = None

    try:
        # 1. Start in local single-user header mode.
        proc, base_url = _spawn_server(tmp_path)
        agent_id = _agent_id(base_url)
        session_id = _create_session(base_url, agent_id)
        assert session_id in _list_session_ids(base_url)
        stop()

        # 2. Restart in accounts mode. The forward migration should hand the
        #    session to the first admin.
        proc, base_url = _spawn_server(
            tmp_path,
            extra_env={
                "OMNIGENT_AUTH_PROVIDER": "accounts",
                "OMNIGENT_AUTH_ENABLED": "1",
                "OMNIGENT_ACCOUNTS_COOKIE_SECRET": secrets.token_hex(32),
                "OMNIGENT_ACCOUNTS_BASE_URL": "http://127.0.0.1:0",
                "OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME": _ADMIN_USERNAME,
                "OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD": _ADMIN_PASSWORD,
            },
        )
        cookies = _login(base_url, _ADMIN_USERNAME, _ADMIN_PASSWORD)
        assert session_id in _list_session_ids(base_url, cookies=cookies)
        stop()

        # 3. Restart back in local single-user mode. The reverse migration
        #    should consolidate ownership under the local sentinel.
        proc, base_url = _spawn_server(tmp_path)
        assert session_id in _list_session_ids(base_url)
        stop()

        # 4. Restart in accounts mode again. The forward migration should find
        #    the local-owned session and hand it back to the admin.
        proc, base_url = _spawn_server(
            tmp_path,
            extra_env={
                "OMNIGENT_AUTH_PROVIDER": "accounts",
                "OMNIGENT_AUTH_ENABLED": "1",
                "OMNIGENT_ACCOUNTS_COOKIE_SECRET": secrets.token_hex(32),
                "OMNIGENT_ACCOUNTS_BASE_URL": "http://127.0.0.1:0",
                "OMNIGENT_ACCOUNTS_INIT_ADMIN_USERNAME": _ADMIN_USERNAME,
                "OMNIGENT_ACCOUNTS_INIT_ADMIN_PASSWORD": _ADMIN_PASSWORD,
            },
        )
        cookies = _login(base_url, _ADMIN_USERNAME, _ADMIN_PASSWORD)
        assert session_id in _list_session_ids(base_url, cookies=cookies)
    finally:
        stop()

    # The sidecar should record the final accounts posture too.
    sidecar = tmp_path / "server-state.json"
    assert sidecar.exists()
    state = json.loads(sidecar.read_text())
    assert state["last_auth_source"] == "accounts"
    assert state["last_local_single_user"] is False
