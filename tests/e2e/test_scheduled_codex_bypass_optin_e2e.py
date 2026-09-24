"""Verify scheduled Codex tasks can persist the unattended bypass mode."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HEALTH_TIMEOUT_S = 60.0
_POLL_INTERVAL_S = 0.5

_CODEX_AGENT_NAME = "codex-native-ui"
_CLAUDE_AGENT_NAME = "claude-native-ui"
_HOURLY_RRULE = "FREQ=HOURLY;INTERVAL=1"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = int(s.getsockname()[1])
    s.close()
    return port


@pytest.fixture()
def scheduled_tasks_server(tmp_path: Path) -> Iterator[httpx.Client]:
    """Run the scheduling API against a loopback server."""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "e2e.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    log_path = tmp_path / "server.log"

    env = {**os.environ}
    apply_server_env(env, _REPO_ROOT)

    log_handle = open(log_path, "w")  # noqa: SIM115 — lives for the Popen lifetime
    proc = subprocess.Popen(
        [
            server_executable(),
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
        ],
        env=env,
        cwd=compat_server_cwd(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    client = httpx.Client(base_url=base_url, trust_env=False, timeout=30)
    try:
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        last_error: object = "not polled yet"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                log_handle.flush()
                raise RuntimeError(
                    f"server exited early (code {proc.returncode}):\n"
                    f"{log_path.read_text()[-3000:]}"
                )
            try:
                resp = client.get("/health")
                if resp.status_code == 200:
                    break
            except httpx.HTTPError as exc:
                last_error = exc
            time.sleep(_POLL_INTERVAL_S)
        else:
            log_handle.flush()
            raise RuntimeError(
                f"server never became healthy within {_HEALTH_TIMEOUT_S:.0f}s "
                f"(last_error={last_error}):\n{log_path.read_text()[-3000:]}"
            )
        yield client
    finally:
        client.close()
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log_handle.close()


def _headers() -> dict[str, str]:
    return {"X-Forwarded-Email": "alice@example.com"}


def _agent_id(client: httpx.Client, name: str) -> str:
    resp = client.get("/v1/agents", headers=_headers())
    resp.raise_for_status()
    by_name = {a["name"]: a["id"] for a in resp.json()["data"]}
    assert name in by_name, f"built-in agent {name!r} not registered (got {sorted(by_name)})"
    return by_name[name]


def _create_task(client: httpx.Client, agent_id: str, name: str, **extra: object) -> dict:
    body: dict[str, object] = {
        "name": name,
        "prompt": "Write ./artifact.txt with the current time.",
        "rrule": _HOURLY_RRULE,
        "agent_id": agent_id,
        "timezone": "UTC",
    }
    body.update(extra)
    resp = client.post("/v1/scheduled-tasks", json=body, headers=_headers())
    assert resp.status_code == 200, f"create {name!r} failed: {resp.status_code} {resp.text}"
    return resp.json()


def test_codex_scheduled_task_can_opt_into_bypass_permissions(
    scheduled_tasks_server: httpx.Client,
) -> None:
    """A codex-native scheduled task must accept the bypassPermissions opt-in."""
    client = scheduled_tasks_server
    codex_id = _agent_id(client, _CODEX_AGENT_NAME)
    claude_id = _agent_id(client, _CLAUDE_AGENT_NAME)

    codex_task = _create_task(client, codex_id, "hourly codex artifact")
    codex_task_id = codex_task["id"]
    assert codex_task["permission_mode"] is None

    patched = client.patch(
        f"/v1/scheduled-tasks/{codex_task_id}",
        json={"permission_mode": "bypassPermissions"},
        headers=_headers(),
    )
    assert patched.status_code == 200, (
        "codex-native scheduled task cannot opt into bypassPermissions "
        f"(HTTP {patched.status_code}: {patched.text}) — the unattended "
        "approval stall has no opt-out."
    )
    assert patched.json()["permission_mode"] == "bypassPermissions"

    got = client.get(f"/v1/scheduled-tasks/{codex_task_id}", headers=_headers())
    assert got.status_code == 200
    assert got.json()["permission_mode"] == "bypassPermissions"

    created_with_bypass = _create_task(
        client, codex_id, "hourly codex artifact (bypass)", permission_mode="bypassPermissions"
    )
    assert created_with_bypass["permission_mode"] == "bypassPermissions"

    rejected = client.patch(
        f"/v1/scheduled-tasks/{codex_task_id}",
        json={"permission_mode": "acceptEdits"},
        headers=_headers(),
    )
    assert rejected.status_code == 400, rejected.text
    assert "permission_mode" in rejected.text

    claude_task = _create_task(client, claude_id, "hourly claude artifact")
    claude_patched = client.patch(
        f"/v1/scheduled-tasks/{claude_task['id']}",
        json={"permission_mode": "bypassPermissions"},
        headers=_headers(),
    )
    assert claude_patched.status_code == 200, claude_patched.text
    assert claude_patched.json()["permission_mode"] == "bypassPermissions"
