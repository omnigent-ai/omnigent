"""A script drives a worker with ``omnigent session create|send|read|get``, end to end.

The CLI runs as its own process, the way a worker-pool script calls it. With
no ``--host-id``, ``create`` starts this machine's host daemon against the
server, binds the new session to that host and the current directory, queues
the task, prints the session id and exits. The runner then answers the task
from the mock LLM, which proves the queued message reached the session's turn.
``send --wait`` then hands the worker a follow-up and returns its reply, and
``read`` and ``get`` report the transcript and the idle session.

Runs against the mock LLM server; no credentials needed::

    .venv/bin/python -m pytest tests/e2e/test_session_create_cli_e2e.py -v
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import yaml

from tests.e2e.conftest import configure_mock_llm, lookup_agent_id, upload_agent
from tests.e2e.test_host_claude_native_e2e import _poll_for_assistant_marker
from tests.e2e.test_host_e2e import _wait_for_host_online, _write_smoke_agent_yaml


def _omnigent(
    args: list[str], *, env: dict[str, str], cwd: Path, stdin: str | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "omnigent", *args],
        input=stdin,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def test_a_script_creates_a_worker_then_messages_and_reads_it(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    marker = f"SESSION_CREATE_CLI_OK_{uuid.uuid4().hex}"
    follow_up = f"SESSION_SEND_CLI_OK_{uuid.uuid4().hex}"
    # Both prompts name the first marker, so both turns draw from this queue in order.
    configure_mock_llm(mock_llm_server_url, [{"text": marker}, {"text": follow_up}], match=marker)
    agent_id = lookup_agent_id(
        http_client, upload_agent(http_client, _write_smoke_agent_yaml(tmp_path))
    )

    # A machine of its own: the shared test server keeps one host row per name.
    home = tmp_path / "home"
    (home / ".omnigent").mkdir(parents=True)
    host_id = uuid.uuid4().hex
    (home / ".omnigent" / "config.yaml").write_text(
        yaml.safe_dump({"host": {"host_id": host_id, "name": f"e2e-cli-{host_id[:12]}"}})
    )
    workspace = tmp_path / "repo"
    workspace.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        # A remote-mode host daemon forwards only the credentials its owner names.
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH": "OPENAI_API_KEY,OPENAI_BASE_URL",
    }
    try:
        created = _omnigent(
            [
                *("session", "create", "--server", live_server, "--agent-id", agent_id),
                *("--title", "CLI worker", "--message-file", "-", "--json"),
            ],
            env=env,
            cwd=workspace,
            stdin=f"Reply with exactly the literal string {marker} and nothing else.",
        )
        assert created.returncode == 0, created.stderr
        result = json.loads(created.stdout)
        session_id = result["session_id"]
        assert result["message_queued"] is True
        assert result["url"].endswith(f"/c/{session_id}")

        _wait_for_host_online(http_client, host_id)
        session = http_client.get(f"/v1/sessions/{session_id}", params={"include_items": "false"})
        session.raise_for_status()
        snapshot = session.json()
        assert (snapshot["host_id"], snapshot["workspace"], snapshot["title"]) == (
            host_id,
            str(workspace.resolve()),
            "CLI worker",
        )

        answer = _poll_for_assistant_marker(
            http_client, session_id=session_id, marker=marker, timeout=180
        )
        assert marker in answer

        on_server = ("--server", live_server)
        sent = _omnigent(
            [
                *("session", "send", session_id, *on_server, "--message-file", "-"),
                *("--wait", "--timeout", "180", "--json"),
            ],
            env=env,
            cwd=workspace,
            stdin=f"Follow-up for {marker}: reply with exactly the literal string {follow_up}.",
        )
        assert sent.returncode == 0, sent.stderr
        report = json.loads(sent.stdout)
        assert (report["status"], report["pending_approvals"]) == ("idle", 0)
        assert follow_up in report["reply"]

        read = _omnigent(
            ["session", "read", session_id, *on_server, "--last", "2", "--json"],
            env=env,
            cwd=workspace,
        )
        assert read.returncode == 0, read.stderr
        latest = json.loads(read.stdout)["messages"]
        assert [message["role"] for message in latest] == ["user", "assistant"]
        assert all(follow_up in message["text"] for message in latest)

        got = _omnigent(
            ["session", "get", session_id, *on_server, "--json"], env=env, cwd=workspace
        )
        assert got.returncode == 0, got.stderr
        info = json.loads(got.stdout)
        assert (info["session_id"], info["status"], info["title"], info["host_id"]) == (
            session_id,
            "idle",
            "CLI worker",
            host_id,
        )
    finally:
        _omnigent(["host", "stop", "--server", live_server], env=env, cwd=workspace)
