"""Exercise missing-workspace refusal through a real server, host, and runner.

After a runner dies and its workspace is deleted, the host refuses relaunch.
The server must retain the structured error without logging a turn-failure
ERROR for this expected condition. The LLM endpoint is mocked.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import tarfile
import time
from pathlib import Path

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.compat import (
    apply_server_env,
    compat_server_cwd,
    server_executable,
)
from tests.e2e.conftest import HEALTH_TIMEOUT_S, POLL_INTERVAL_S, find_free_port
from tests.e2e.test_host_e2e import (
    _pid_alive,
    _spawn_host_daemon,
    _wait_for_host_online,
)
from tests.e2e.test_host_runner_leak_5182 import (
    _launches,
    _runner_online,
    _wait_for,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Runners start in the workspace, so their import roots must be absolute.
_SOURCE_ROOTS = (
    _REPO_ROOT,
    _REPO_ROOT / "sdks" / "python-client",
    _REPO_ROOT / "sdks" / "ui",
)

_KPI_TURN_FAILED_PREFIX = "session turn failed for "


def _ensure_worktree_on_pythonpath() -> None:
    """Prepend this checkout's absolute roots to runner subprocess PYTHONPATH.

    The daemon passes PYTHONPATH through, but runners start in the workspace.
    """
    existing = os.environ.get("PYTHONPATH", "")
    parts = existing.split(os.pathsep) if existing else []
    roots = [str(root) for root in _SOURCE_ROOTS]
    if all(root in parts for root in roots):
        return
    prepend = [root for root in roots if root not in parts]
    os.environ["PYTHONPATH"] = os.pathsep.join([*prepend, *parts])


_AGENT_YAML = "\n".join(
    [
        "name: ws-missing-repro-agent",
        "description: Minimal agent for the workspace-missing repro.",
        "executor:",
        "  harness: openai-agents",
        "  model: gpt-5.4",
        "prompt: |",
        "  You are a terse smoke-test assistant.",
        "  Follow the user's instruction exactly.",
        "",
    ]
)


def _spawn_server(
    *, tmp_path: Path, mock_llm_server_url: str
) -> tuple[subprocess.Popen, str, Path]:
    """Spawn a server accepting host runner tokens with a dedicated log path."""
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    tmp_path.mkdir(parents=True, exist_ok=True)
    db_path = tmp_path / "ws_missing.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    server_log = tmp_path / "server.log"

    env = {
        **os.environ,
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
    }
    apply_server_env(env, _REPO_ROOT)
    # Host-generated per-launch runner tokens must be accepted.
    env.pop("OMNIGENT_RUNNER_TUNNEL_TOKEN", None)

    log_handle = open(server_log, "w")  # noqa: SIM115 — lives for the Popen lifetime; closed by the caller
    proc = subprocess.Popen(
        [
            server_executable(),
            "-m",
            "omnigent.cli",
            "server",
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
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        if proc.poll() is not None:
            break
        time.sleep(POLL_INTERVAL_S)
    else:
        proc.kill()
        log_handle.close()
        raise RuntimeError(
            f"server didn't pass health within {HEALTH_TIMEOUT_S}s; "
            f"log tail:\n{server_log.read_text()[-3000:]}"
        )
    if proc.poll() is not None:
        log_handle.close()
        raise RuntimeError(
            f"server exited early (code {proc.returncode}); "
            f"log tail:\n{server_log.read_text()[-3000:]}"
        )
    return proc, base_url, server_log


def _register_agent(client: httpx.Client) -> str:
    """Register the smoke agent from its bundle."""
    yaml_bytes = _AGENT_YAML.encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo("agent.yaml")
        info.size = len(yaml_bytes)
        tar.addfile(info, io.BytesIO(yaml_bytes))
    resp = client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    resp.raise_for_status()
    session_id = resp.json()["session_id"]
    agent_resp = client.get(f"/v1/sessions/{session_id}/agent")
    agent_resp.raise_for_status()
    return agent_resp.json()["id"]


def _error_items(client: httpx.Client, session_id: str) -> list[dict]:
    """Return the transcript's ``type=error`` items for a session."""
    resp = client.get(f"/v1/sessions/{session_id}/items")
    resp.raise_for_status()
    return [item for item in resp.json()["data"] if item.get("type") == "error"]


@pytest.mark.timeout(600)
def test_missing_workspace_relaunch_is_not_logged_as_turn_failure(
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """Preserve the error item without logging the refusal as an ERROR."""
    server_proc: subprocess.Popen | None = None
    server_log: Path | None = None
    daemon = None
    gen1_pid: int | None = None
    try:
        _ensure_worktree_on_pythonpath()

        server_proc, base_url, server_log = _spawn_server(
            tmp_path=tmp_path / "server",
            mock_llm_server_url=mock_llm_server_url,
        )
        client = httpx.Client(
            base_url=base_url,
            timeout=120,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )

        daemon = _spawn_host_daemon(
            tmp_path=tmp_path / "host",
            live_server=base_url,
            mock_llm_server_url=mock_llm_server_url,
        )
        _wait_for_host_online(client, daemon.host_id, timeout=60.0)

        agent_id = _register_agent(client)

        # The first launch proves the workspace existed before deletion.
        workspace = tmp_path / "worktree" / "universe"
        workspace.mkdir(parents=True)

        create = client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_id,
                "host_id": daemon.host_id,
                "workspace": str(workspace),
            },
            timeout=90.0,
        )
        create.raise_for_status()
        session_id = create.json()["id"]
        runner_id = create.json()["runner_id"]
        assert runner_id is not None, create.text

        _gen1_id, gen1_pid = _wait_for(
            lambda: (_launches(daemon.daemon_log) or [None])[0],
            timeout=60.0,
            what="the host daemon to log gen1's launch",
        )
        _wait_for(
            lambda: _runner_online(client, runner_id),
            timeout=90.0,
            what=f"gen1 runner {runner_id} to connect its tunnel",
        )
        assert _pid_alive(gen1_pid), f"gen1 (pid={gen1_pid}) died before it was superseded"

        # Force the next message to relaunch an offline runner.
        os.kill(gen1_pid, signal.SIGKILL)
        _wait_for(
            lambda: not _runner_online(client, runner_id),
            timeout=120.0,
            what=f"the server to declare runner {runner_id} offline after it was killed",
        )

        shutil.rmtree(workspace)
        assert not workspace.exists()

        # The host should refuse relaunch because the workspace is gone.
        msg = client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [{"type": "input_text", "text": "are you still there?"}],
                },
            },
            timeout=120.0,
        )
        assert msg.status_code in (200, 202), f"unexpected status: {msg.status_code}: {msg.text}"

        # The user still gets the sanitized refusal.
        error_items = _wait_for(
            lambda: _error_items(client, session_id) or None,
            timeout=60.0,
            what="the server to persist the workspace-missing error item",
        )
        assert len(error_items) == 1, f"expected exactly one error item, got {error_items!r}"
        err = error_items[0]
        assert err["code"] == "workspace_missing", err
        assert err["message"] == f"workspace path does not exist: {workspace}", err

        # The refusal must remain observable in server logs.
        log_text = server_log.read_text()
        assert str(workspace) in log_text, (
            "the server never logged the workspace-missing refusal at all"
        )

        # An earlier runner crash may log a separate, legitimate turn failure.
        # Only the expected workspace_missing signature must be absent.
        offending = [
            line
            for line in log_text.splitlines()
            if f"{_KPI_TURN_FAILED_PREFIX}{session_id}" in line and "workspace_missing" in line
        ]
        assert offending == [], (
            "an expected workspace-missing host refusal was logged as a "
            "turn failure (KPI-counted). Offending server log line(s):\n" + "\n".join(offending)
        )
    finally:
        if gen1_pid is not None and _pid_alive(gen1_pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(gen1_pid, signal.SIGKILL)
        if daemon is not None:
            daemon.proc.send_signal(signal.SIGTERM)
            try:
                daemon.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                daemon.proc.kill()
                daemon.proc.wait()
        if server_proc is not None:
            if server_proc.poll() is None:
                server_proc.send_signal(signal.SIGTERM)
                try:
                    server_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    server_proc.kill()
                    server_proc.wait()
