"""Project setup commands against a real server, host daemon and git worktree.

``env_commands`` call a function from the user's ``.bashrc`` (as ``conda
activate`` relies on ``conda init``); the agent's shell tool must see the
environment it sets. ``setup_commands`` run once per worktree in the
background and are not re-run by a relaunch. A broken env command is reported
in the transcript without blocking the turn.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests._helpers.host import Host, isolated_host
from tests._helpers.polling import poll_until
from tests._helpers.session import bind_session_runner
from tests.e2e.conftest import (
    configure_mock_llm,
    lookup_agent_id,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)
from tests.e2e.helpers import get_output_items

pytestmark = [
    pytest.mark.min_server_version("0.21.0"),
    pytest.mark.min_runner_version("0.21.0"),
    pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash"),
]

_SHELL_RC = (
    'fakeenv_activate() { export FAKEENV_NAME="$1"; export PATH="$HOME/fakeenv/bin:$PATH"; }\n'
)
_PROBE = (
    'echo "FAKEENV=$FAKEENV_NAME PATH=$PATH SETUP=$(cat setup-done 2>/dev/null)'
    ' APPLIED=$OMNIGENT_PROJECT_ENV_APPLIED"'
)


def _repo_with_worktree(root: Path) -> tuple[Path, Path]:
    """Create a repo with one linked worktree.

    :returns: ``(main checkout, worktree)``.
    """
    repo, worktree = root / "repo", root / "repo-worktrees" / "feature"
    repo.mkdir(parents=True)
    identity = ["-c", "user.email=e2e@test", "-c", "user.name=e2e"]
    for args in (
        ["init", "-q", "-b", "main"],
        [*identity, "commit", "-q", "--allow-empty", "-m", "init"],
        ["worktree", "add", "-q", "-b", "feature", str(worktree)],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo, worktree


def _register_shell_agent(client: httpx.Client, mock_llm_server_url: str) -> tuple[str, str]:
    """Register an agent whose mock model calls ``sys_os_shell``.

    :returns: ``(agent_id, mock model key)``.
    """
    model = f"mock-project-setup-{uuid.uuid4().hex[:6]}"
    name = register_inline_agent(
        client,
        name=f"project-setup-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="Call sys_os_shell with exactly the command requested.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={
            "os_env": {
                "type": "caller_process",
                "cwd": ".",
                "sandbox": {"type": "none", "allow_network": True},
            }
        },
    )
    return lookup_agent_id(client, name), model


def _probe_env(client: httpx.Client, mock_llm_server_url: str, model: str, session_id: str) -> str:
    """Run one turn whose tool call prints the agent's env; return its stdout."""
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": uuid.uuid4().hex[:8],
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": _PROBE}),
                    }
                ]
            },
            {"text": "done"},
        ],
        key=model,
    )
    response_id = send_user_message_to_session(client, session_id=session_id, content="probe")
    body = poll_session_until_terminal(
        client, session_id=session_id, response_id=response_id, timeout=120
    )
    assert body["status"] == "completed", body.get("error")
    (output,) = get_output_items(body, "function_call_output")
    return str(json.loads(output["output"])["stdout"])


def _runner_online(client: httpx.Client, runner_id: str) -> bool:
    status = client.get(f"/v1/runners/{runner_id}/status")
    return status.status_code == 200 and status.json().get("online") is True


def _kill_session_runner(client: httpx.Client, host: Host, session_id: str) -> None:
    """SIGKILL the session's runner so the next message relaunches it."""
    runner_id = client.get(f"/v1/sessions/{session_id}").json()["runner_id"]
    os.kill(host.runner_pid(runner_id), signal.SIGKILL)
    poll_until(lambda: not _runner_online(client, runner_id), timeout_s=15)


def _notice(client: httpx.Client, session_id: str, code: str) -> dict[str, Any] | None:
    items = client.get(f"/v1/sessions/{session_id}/items").json()["data"]
    return next((i for i in items if i.get("type") == "error" and i["code"] == code), None)


def test_project_setup_applies_to_a_real_host_session(
    live_server: str, http_client: httpx.Client, tmp_path: Path, mock_llm_server_url: str
) -> None:
    repo, worktree = _repo_with_worktree(tmp_path / "src")
    agent_id, model = _register_shell_agent(http_client, mock_llm_server_url)
    setup_commands = [
        'echo "$OMNIGENT_ROOT_PATH" > setup-root.txt',
        "echo ran >> setup-runs.txt",
        "sleep 3 && echo yes > setup-done",
    ]
    project = http_client.post(
        "/v1/projects",
        json={
            "name": f"setup-e2e-{uuid.uuid4().hex[:6]}",
            "config": {
                "env_commands": ["fakeenv_activate proj-e2e"],
                "setup_commands": setup_commands,
            },
        },
    )
    assert project.status_code == 200, project.text
    project_id = project.json()["id"]
    home = tmp_path / "host" / "home"
    home.mkdir(parents=True)
    (home / ".bashrc").write_text(_SHELL_RC)
    (home / ".bash_profile").write_text("source ~/.bashrc\n")

    with isolated_host(
        tmp_path / "host",
        live_server,
        env={
            "SHELL": shutil.which("bash"),
            "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
            "OPENAI_API_KEY": "mock-key",
        },
    ) as host:
        host.wait_online(http_client, timeout=45, poll_interval=0.1)
        session = http_client.post(
            "/v1/sessions", json={"agent_id": agent_id, "project_id": project_id}
        )
        assert session.status_code == 201, session.text
        session_id = session.json()["id"]
        launch = http_client.post(
            f"/v1/hosts/{host.host_id}/runners",
            json={"session_id": session_id, "workspace": str(worktree)},
            timeout=90,
        )
        assert launch.status_code == 200, launch.text
        runner_id = launch.json()["runner_id"]
        poll_until(lambda: _runner_online(http_client, runner_id), timeout_s=60)
        bind_session_runner(http_client.patch, live_server, session_id, runner_id)

        # First launch: the env reaches the agent, whose turn waits for setup.
        stdout = _probe_env(http_client, mock_llm_server_url, model, session_id)
        assert "FAKEENV=proj-e2e" in stdout and f"{home}/fakeenv/bin:" in stdout, stdout
        assert "SETUP=yes" in stdout and "APPLIED=1" in stdout, stdout
        poll_until(
            lambda: _notice(http_client, session_id, "project_setup_finished"), timeout_s=60
        )
        assert _notice(http_client, session_id, "project_setup_running") is not None
        assert (worktree / "setup-root.txt").read_text().strip() == str(repo)

        # Relaunch: the env still applies and the worktree setup does not re-run.
        _kill_session_runner(http_client, host, session_id)
        stdout = _probe_env(http_client, mock_llm_server_url, model, session_id)
        assert "FAKEENV=proj-e2e" in stdout, stdout
        assert (worktree / "setup-runs.txt").read_text().split() == ["ran"]

        # A broken env command is reported and the turn still runs, without the env.
        http_client.patch(
            f"/v1/projects/{project_id}",
            json={
                "config": {
                    "env_commands": ["fakeenv_missing proj-e2e"],
                    "setup_commands": setup_commands,
                }
            },
        ).raise_for_status()
        _kill_session_runner(http_client, host, session_id)
        stdout = _probe_env(http_client, mock_llm_server_url, model, session_id)
        assert "FAKEENV= " in stdout, stdout
        failed = _notice(http_client, session_id, "project_setup_failed")
        assert failed is not None and failed["level"] == "error", failed
        # The notice names the exit code and host log, never the command output.
        assert "exit 127" in failed["message"] and "log:" in failed["message"], failed

        # "Re-run setup" runs it again on the host while the same runner keeps going.
        runner_before = http_client.get(f"/v1/sessions/{session_id}").json()["runner_id"]
        rerun = http_client.post(f"/v1/sessions/{session_id}/project-setup/rerun")
        assert rerun.status_code == 202, rerun.text
        poll_until(
            lambda: (worktree / "setup-runs.txt").read_text().split() == ["ran", "ran"],
            timeout_s=60,
        )
        assert http_client.get(f"/v1/sessions/{session_id}").json()["runner_id"] == runner_before
