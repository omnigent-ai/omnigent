"""End-to-end coverage for host-managed fixed git worktree pools."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml

from tests._helpers.compat import (
    apply_server_env,
    compat_server_cwd,
    server_executable,
)
from tests.e2e.conftest import (
    configure_mock_llm,
    find_free_port,
    get_mock_requests,
    lookup_agent_id,
    poll_session_until_terminal,
    send_user_message_to_session,
    upload_agent,
    wait_for_server,
)
from tests.e2e.helpers import POLL_INTERVAL_S, final_assistant_text
from tests.e2e.test_host_e2e import (
    _pid_alive,
    _spawn_host_daemon,
    _wait_for_host_online,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture()
def pool_live_server(
    tmp_path: Path,
    mock_llm_server_url: str,
) -> Iterator[str]:
    """Start an isolated local server with fast pooled-runner eviction."""
    port = find_free_port()
    db_path = tmp_path / "pool-e2e.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    server_log = tmp_path / "pool-server.log"
    server_cfg = tmp_path / "pool-server.yaml"
    server_cfg.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "model": "_policy_llm_",
                    "connection": {
                        "base_url": f"{mock_llm_server_url}/v1",
                        "api_key": "mock-key",
                    },
                }
            }
        )
    )
    env = {
        **os.environ,
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OMNIGENT_WORKTREE_POOL_IDLE_EVICTION_S": "1",
    }
    apply_server_env(env, _REPO_ROOT)
    log_handle = open(server_log, "w")  # noqa: SIM115
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
            "--config",
            str(server_cfg),
        ],
        env=env,
        cwd=compat_server_cwd(),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://localhost:{port}"
    try:
        wait_for_server(base_url, timeout=30.0)
    except Exception as exc:
        log_contents = server_log.read_text() if server_log.exists() else ""
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        log_handle.close()
        raise AssertionError(f"pool e2e server failed; log:\n{log_contents[-4000:]}") from exc
    try:
        yield base_url
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        log_handle.close()


@pytest.fixture()
def pool_http_client(pool_live_server: str) -> Iterator[httpx.Client]:
    """HTTP client for the isolated pool e2e server."""
    with httpx.Client(base_url=pool_live_server, timeout=120) as client:
        yield client


def _write_smoke_agent_yaml(tmp_path: Path) -> Path:
    """Create a minimal mock-backed agent bundle."""
    agent_dir = tmp_path / f"pool-agent-{uuid.uuid4().hex[:8]}"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "\n".join(
            [
                f"name: {agent_dir.name}",
                "description: Minimal agent for worktree-pool e2e tests.",
                "executor:",
                "  harness: openai-agents",
                "  model: gpt-5.4",
                "os_env:",
                "  cwd: .",
                "prompt: |",
                "  You are only used to start a runner tunnel in tests.",
                "",
            ]
        )
    )
    return agent_dir


def _init_git_repo(path: Path) -> None:
    """Create a tiny git repository with a ``main`` branch."""
    path.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "pool-e2e@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Pool E2E"], cwd=path, check=True)
    (path / "README.md").write_text("pool e2e\n")
    subprocess.run(["git", "add", "README.md"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)


def _add_origin(repo: Path) -> Path:
    """Add a local bare ``origin`` remote and push ``main``."""
    remote = repo.parent / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "remote", "add", "origin", str(remote)], cwd=repo, check=True)
    subprocess.run(
        ["git", "push", "-u", "origin", "main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    return remote


def _git_bare(repo: Path, *args: str) -> str:
    """Run a git command against a bare repository."""
    result = subprocess.run(
        ["git", "--git-dir", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _create_session(client: httpx.Client, agent_id: str) -> str:
    """Create an unbound session and return its id."""
    resp = client.post("/v1/sessions", json={"agent_id": agent_id}, timeout=60.0)
    resp.raise_for_status()
    return str(resp.json()["id"])


def _wait_for_runner_online(client: httpx.Client, runner_id: str, timeout: float = 30.0) -> None:
    """Poll until a host-spawned runner tunnel is online."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/runners/{runner_id}/status", timeout=5.0)
        if resp.status_code == 200 and resp.json().get("online") is True:
            return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"runner {runner_id!r} did not come online")


def _wait_for_runner_offline(client: httpx.Client, runner_id: str, timeout: float = 15.0) -> None:
    """Poll until a killed runner tunnel is no longer reachable."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/runners/{runner_id}/status", timeout=5.0)
        if resp.status_code != 200 or resp.json().get("online") is not True:
            return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"runner {runner_id!r} remained online")


def _wait_for_new_runner(
    client: httpx.Client,
    session_id: str,
    previous_runner_id: str | None,
    timeout: float = 30.0,
) -> str:
    """Poll until a resume rotates the session to a connected runner."""
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}", timeout=5.0)
        resp.raise_for_status()
        last = resp.json()
        runner_id = last.get("runner_id")
        if isinstance(runner_id, str) and runner_id != previous_runner_id:
            status = client.get(f"/v1/runners/{runner_id}/status", timeout=5.0)
            if status.status_code == 200 and status.json().get("online") is True:
                return runner_id
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"session {session_id!r} did not bind a new runner; last={last}")


def _latest_runner_pid(log_path: Path) -> int:
    """Return the most recently logged host-spawned runner PID."""
    matches = re.findall(
        r"Launched runner \S+ for workspace .*? \(pid=(\d+)\)",
        log_path.read_text(),
    )
    if not matches:
        raise AssertionError(f"no runner PID found in {log_path}: {log_path.read_text()}")
    return int(matches[-1])


def _run_turn(
    client: httpx.Client,
    *,
    session_id: str,
    prompt: str,
    expected: str,
) -> None:
    """Send one turn and wait for the expected assistant marker."""
    response_id = send_user_message_to_session(client, session_id=session_id, content=prompt)
    body = poll_session_until_terminal(
        client,
        session_id=session_id,
        response_id=response_id,
        timeout=120,
    )
    assert body["status"] == "completed", body
    assert expected in final_assistant_text(body), body


def _wait_for_session_evicted(
    client: httpx.Client,
    session_id: str,
    host_id: str,
    workspace: str,
    git_branch: str,
    timeout: float = 30.0,
) -> dict:
    """Poll until idle eviction clears only the session's runner binding."""
    deadline = time.monotonic() + timeout
    last: dict | None = None
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}", timeout=5.0)
        resp.raise_for_status()
        last = resp.json()
        labels = last.get("labels") or {}
        if (
            last.get("runner_id") is None
            and last.get("host_id") == host_id
            and last.get("workspace") == workspace
            and last.get("git_branch") == git_branch
            and "omnigent.worktree_pool.lease_id" not in labels
        ):
            return last
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"session {session_id!r} was not evicted; last={last}")


def test_host_worktree_pool_capacity_eviction_and_reuse(
    pool_live_server: str,
    pool_http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """Real server and host exercise new, warm-resume, and cold-resume paths."""
    first_prompt = "POOL_FIRST_TURN_CONTEXT_ALPHA"
    first_reply = "POOL_FIRST_REPLY_ALPHA"
    warm_prompt = "POOL_WARM_RESUME_CONTEXT_BETA"
    warm_reply = "POOL_WARM_REPLY_BETA"
    cold_prompt = "POOL_COLD_RESUME_CONTEXT_GAMMA"
    cold_reply = "POOL_COLD_REPLY_GAMMA"
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"text": first_reply},
            {"text": warm_reply},
            {"text": cold_reply},
        ],
        key="gpt-5.4",
    )
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    remote = _add_origin(repo)
    slot = tmp_path / "managed-slot-1"
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(slot), "main"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    updater = tmp_path / "base-updater"
    subprocess.run(
        ["git", "clone", "-q", "-b", "main", str(remote), str(updater)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "pool-e2e@example.com"], cwd=updater, check=True
    )
    subprocess.run(["git", "config", "user.name", "Pool E2E"], cwd=updater, check=True)
    Path(updater, "latest-base.txt").write_text("fetched before session launch\n")
    subprocess.run(["git", "add", "latest-base.txt"], cwd=updater, check=True)
    subprocess.run(
        ["git", "commit", "-m", "advance managed base"],
        cwd=updater,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "push", "origin", "main"], cwd=updater, check=True, capture_output=True)
    latest_base = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=updater,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert (
        subprocess.run(
            ["git", "rev-parse", "origin/main"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        != latest_base
    )
    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=pool_live_server,
        mock_llm_server_url=mock_llm_server_url,
        host_config={
            "managed_worktrees": {
                "idle_eviction_seconds": 5,
                "repos": {
                    "universe": {
                        "base_branch": "origin/main",
                        "branch_remote": "origin",
                        "worktrees": [str(slot)],
                    }
                },
            }
        },
    )
    host_proc = daemon.proc

    try:
        _wait_for_host_online(pool_http_client, daemon.host_id, timeout=30.0)
        deadline = time.monotonic() + 15.0
        refreshed_base = ""
        while time.monotonic() < deadline:
            refreshed_base = subprocess.run(
                ["git", "rev-parse", "origin/main"],
                cwd=repo,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if refreshed_base == latest_base:
                break
            time.sleep(POLL_INTERVAL_S)
        assert refreshed_base == latest_base
        agent_name = upload_agent(pool_http_client, _write_smoke_agent_yaml(tmp_path))
        agent_id = lookup_agent_id(pool_http_client, agent_name)

        first_session = _create_session(pool_http_client, agent_id)
        first_launch = pool_http_client.post(
            f"/v1/hosts/{daemon.host_id}/runners",
            json={
                "session_id": first_session,
                "managed_repo": "universe",
                "git": {
                    "branch_name": "pool-e2e/first",
                    "base_branch": "main",
                },
            },
            timeout=60.0,
        )
        first_launch.raise_for_status()
        first_runner = str(first_launch.json()["runner_id"])
        _wait_for_runner_online(pool_http_client, first_runner)

        first_snapshot = pool_http_client.get(f"/v1/sessions/{first_session}").json()
        assert first_snapshot["workspace"] == "managed://universe"
        assert first_snapshot["git_branch"] == "pool-e2e/first"
        assert Path(slot, "latest-base.txt").read_text() == "fetched before session launch\n"
        _run_turn(
            pool_http_client,
            session_id=first_session,
            prompt=first_prompt,
            expected=first_reply,
        )
        Path(slot, "agent-output.txt").write_text("saved before cleanup")

        blocked_session = _create_session(pool_http_client, agent_id)
        blocked_launch = pool_http_client.post(
            f"/v1/hosts/{daemon.host_id}/runners",
            json={
                "session_id": blocked_session,
                "managed_repo": "universe",
                "git": {
                    "branch_name": "pool-e2e/blocked",
                    "base_branch": "main",
                },
            },
            timeout=60.0,
        )
        assert blocked_launch.status_code == 409, blocked_launch.text
        assert "no available slots" in blocked_launch.text

        first_pid = _latest_runner_pid(daemon.daemon_log)
        os.kill(first_pid, signal.SIGKILL)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and _pid_alive(first_pid):
            time.sleep(POLL_INTERVAL_S)
        assert not _pid_alive(first_pid), f"runner pid {first_pid} did not die"
        _wait_for_runner_offline(pool_http_client, first_runner)

        warm_turn = send_user_message_to_session(
            pool_http_client,
            session_id=first_session,
            content=warm_prompt,
        )
        warm_runner = _wait_for_new_runner(pool_http_client, first_session, first_runner)
        warm_body = poll_session_until_terminal(
            pool_http_client,
            session_id=first_session,
            response_id=warm_turn,
            timeout=120,
        )
        assert warm_body["status"] == "completed", warm_body
        assert warm_reply in final_assistant_text(warm_body), warm_body
        assert Path(slot, "agent-output.txt").read_text() == "saved before cleanup"
        warm_requests = get_mock_requests(mock_llm_server_url, key="gpt-5.4")
        assert len(warm_requests) >= 2, warm_requests
        warm_input = str(warm_requests[-1])
        assert first_prompt in warm_input
        assert first_reply in warm_input

        warm_pid = _latest_runner_pid(daemon.daemon_log)
        assert warm_pid != first_pid
        os.kill(warm_pid, signal.SIGKILL)
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline and _pid_alive(warm_pid):
            time.sleep(POLL_INTERVAL_S)
        assert not _pid_alive(warm_pid), f"runner pid {warm_pid} did not die"
        _wait_for_runner_offline(pool_http_client, warm_runner)
        _wait_for_session_evicted(
            pool_http_client,
            first_session,
            daemon.host_id,
            "managed://universe",
            "pool-e2e/first",
        )
        assert (
            _git_bare(remote, "show", "refs/heads/pool-e2e/first:agent-output.txt")
            == "saved before cleanup"
        )

        cold_turn = send_user_message_to_session(
            pool_http_client,
            session_id=first_session,
            content=cold_prompt,
        )
        cold_runner = _wait_for_new_runner(pool_http_client, first_session, None)
        cold_body = poll_session_until_terminal(
            pool_http_client,
            session_id=first_session,
            response_id=cold_turn,
            timeout=120,
        )
        assert cold_body["status"] == "completed", cold_body
        assert cold_reply in final_assistant_text(cold_body), cold_body
        assert cold_runner != warm_runner
        cold_snapshot = pool_http_client.get(f"/v1/sessions/{first_session}").json()
        assert cold_snapshot["workspace"] == "managed://universe"
        assert cold_snapshot["git_branch"] == "pool-e2e/first"
        assert Path(slot, "agent-output.txt").read_text() == "saved before cleanup"
        cold_requests = get_mock_requests(mock_llm_server_url, key="gpt-5.4")
        assert len(cold_requests) >= 3, cold_requests
        cold_input = str(cold_requests[-1])
        assert first_prompt in cold_input
        assert first_reply in cold_input
        assert warm_prompt in cold_input
        assert warm_reply in cold_input

    finally:
        if host_proc.poll() is None:
            host_proc.send_signal(signal.SIGTERM)
            try:
                host_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                host_proc.kill()
                host_proc.wait(timeout=5)
