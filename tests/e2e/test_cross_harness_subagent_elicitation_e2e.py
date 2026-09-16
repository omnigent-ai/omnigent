"""Real native sessions forward a cross-harness elicitation with mock models.

Codex creates a Claude child through MCP. Claude calls AskUserQuestion via
its real permission hook, and the parent snapshot exposes the child's card.
The answer must reach Claude's next Messages API request as a tool result.

The model APIs are mocked; the server, runner, native CLIs, and hooks are real.
No credentials, global config changes, or live model calls are required::

    uv run --no-sync pytest -o addopts='' \
        tests/e2e/test_cross_harness_subagent_elicitation_e2e.py -v
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    find_free_port,
    get_mock_requests,
    upload_agent,
)

pytestmark = pytest.mark.timeout(360, method="signal")
_REPO = Path(__file__).resolve().parents[2]
_PARENT_MODEL = "mock-codex-parent"
_CHILD_MODEL = "claude-sonnet-4-6"
_QUESTION = "Which color should we use?"
_CHILD_DONE = "CLAUDE_CHILD_RESUMED"


@pytest.fixture
def cross_harness_rig(
    isolated_mock_llm_server_url: str,
    tmp_path: Path,
) -> Iterator[tuple[httpx.Client, Path, str, str]]:
    """Run an isolated server/runner with both native CLIs using the mock API."""
    from omnigent.runner.identity import token_bound_runner_id

    for binary in ("codex", "claude", "tmux"):
        if shutil.which(binary) is None:
            pytest.skip(f"requires the real {binary} binary")
    if any(path.is_file() for path in CLAUDE_CODE_MANAGED_SETTINGS_PATHS):
        pytest.skip("machine-managed Claude settings override mock auth; run in a clean container")
    mock_url = isolated_mock_llm_server_url
    workspace = tmp_path / "workspace"
    config_dir = tmp_path / "config"
    native_home = tmp_path / "home"
    codex_home = native_home / ".codex"
    claude_home = native_home / ".claude"
    for directory in (workspace, config_dir, codex_home, claude_home):
        directory.mkdir(parents=True)
    (config_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "test-models": {
                        "kind": "key",
                        "default": ["openai", "anthropic"],
                        "openai": {
                            "base_url": f"{mock_url}/v1",
                            "api_key": "mock-key",
                            "wire_api": "responses",
                            "models": {"default": _PARENT_MODEL},
                        },
                        "anthropic": {
                            "base_url": mock_url,
                            "api_key": "mock-key",
                            "models": {"default": _CHILD_MODEL},
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    token = uuid.uuid4().hex
    runner_id = token_bound_runner_id(token)
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {
        **os.environ,
        "HOME": str(native_home),
        "CODEX_HOME": str(codex_home),
        "OMNIGENT_CONFIG_HOME": str(config_dir),
        "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
        "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "OMNIGENT_SKIP_WEB_UI": "true",
        "OMNIGENT_CODEX_PATH": str(shutil.which("codex")),
        "OMNIGENT_CLAUDE_PATH": str(shutil.which("claude")),
        "PYTHONPATH": str(_REPO),
    }
    for key in (
        "CLAUDECODE",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE",
        "CODEX",
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "DATABRICKS_TOKEN",
        "DATABRICKS_CONFIG_PROFILE",
    ):
        env.pop(key, None)
    for key in ("NO_PROXY", "no_proxy"):
        env[key] = "127.0.0.1,localhost"
    processes: list[subprocess.Popen[bytes]] = []
    with (
        (tmp_path / "server.log").open("w") as server_log,
        (tmp_path / "runner.log").open("w") as runner_log,
        httpx.Client(
            base_url=base_url,
            timeout=30,
            trust_env=False,
            headers={"x-omnigent-background-session-titles": "off"},
        ) as client,
    ):
        try:
            processes.append(
                subprocess.Popen(
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
                        f"sqlite:///{tmp_path / 'test.db'}",
                        "--artifact-location",
                        str(tmp_path / "artifacts"),
                    ],
                    cwd=_REPO,
                    env={**env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": token},
                    stdout=server_log,
                    stderr=subprocess.STDOUT,
                )
            )
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-m", "omnigent.runner._entry"],
                    cwd=_REPO,
                    env={
                        **env,
                        "OMNIGENT_RUNNER_ID": runner_id,
                        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": token,
                        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                        "RUNNER_SERVER_URL": base_url,
                    },
                    stdout=runner_log,
                    stderr=subprocess.STDOUT,
                )
            )
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                assert all(proc.poll() is None for proc in processes), "server/runner exited"
                with contextlib.suppress(httpx.HTTPError):
                    response = client.get(f"/v1/runners/{runner_id}/status", timeout=2)
                    if response.status_code == 200 and response.json().get("online"):
                        break
                time.sleep(0.5)
            else:
                pytest.fail("server/runner did not become ready")
            yield client, workspace, runner_id, mock_url
        finally:
            for proc in reversed(processes):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)


def _snapshot(client: httpx.Client, session_id: str) -> dict[str, Any]:
    response = client.get(f"/v1/sessions/{session_id}")
    response.raise_for_status()
    return response.json()


def _items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    response = client.get(
        f"/v1/sessions/{session_id}/items", params={"order": "asc", "limit": 1000}
    )
    response.raise_for_status()
    return [
        {**item, **item["data"]} if isinstance(item.get("data"), dict) else item
        for item in response.json()["data"]
    ]


def _wait_for(
    probe: Callable[[], Any],
    *,
    description: str,
    client: httpx.Client,
    session_ids: list[str],
    timeout: float = 180,
) -> Any:
    """Poll real processes and retain session diagnostics on timeout."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(1)
    diagnostics = {
        sid: {"snapshot": _snapshot(client, sid), "items": _items(client, sid)}
        for sid in session_ids
    }
    pytest.fail(f"Timed out waiting for {description}: {json.dumps(diagnostics, default=str)}")


def test_codex_parent_answers_real_claude_child_elicitation(
    cross_harness_rig: tuple[httpx.Client, Path, str, str],
    tmp_path: Path,
) -> None:
    """A different parent harness preserves the child's prompt and native answer."""
    client, workspace, runner_id, mock_url = cross_harness_rig
    name = f"cross-harness-parent-{uuid.uuid4().hex[:8]}"
    parent = {
        "spec_version": 1,
        "name": name,
        "executor": {
            "type": "omnigent",
            "auth": {"type": "provider", "name": "test-models"},
            "config": {"harness": "codex-native", "yolo": True},
        },
        "llm": {"model": _PARENT_MODEL},
        "prompt": "Delegate the color question to claude_code using sys_session_send.",
        "async": True,
        "os_env": {
            "type": "caller_process",
            "cwd": str(workspace),
            "sandbox": {"type": "none"},
        },
        "tools": {"agents": ["claude_code"]},
    }
    child = {
        "spec_version": 1,
        "name": "claude_code",
        "executor": {
            "type": "omnigent",
            "auth": {"type": "provider", "name": "test-models"},
            "config": {"harness": "claude-native", "permission_mode": "default"},
        },
        "llm": {"model": _CHILD_MODEL},
        "prompt": "Ask the user the color question using AskUserQuestion.",
        "os_env": parent["os_env"],
    }
    bundle = tmp_path / name
    child_dir = bundle / "agents" / "claude_code"
    child_dir.mkdir(parents=True)
    (bundle / "config.yaml").write_text(yaml.safe_dump(parent), encoding="utf-8")
    (child_dir / "config.yaml").write_text(yaml.safe_dump(child), encoding="utf-8")
    # Native and server title generation must not consume the child's tool call.
    for title_marker in ("<session>", "<user_message>"):
        configure_mock_llm(mock_url, [{"text": "Color question"}], match=title_marker)
    configure_mock_llm(
        mock_url,
        [
            {
                "text": "",
                "native_items": [
                    {
                        "type": "function_call",
                        "id": "fc-dispatch-claude",
                        "call_id": "dispatch-claude",
                        "namespace": "mcp__omnigent",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "claude_code",
                                "title": "color-question",
                                "args": "Ask the user the color question.",
                            }
                        ),
                    }
                ],
            },
            {"text": "Waiting for the Claude child."},
        ],
        key=_PARENT_MODEL,
    )
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "ask-color",
                        "name": "AskUserQuestion",
                        "arguments": json.dumps(
                            {
                                "questions": [
                                    {
                                        "question": _QUESTION,
                                        "header": "Color",
                                        "multiSelect": False,
                                        "options": [
                                            {"label": "Red", "description": "Use red."},
                                            {"label": "Blue", "description": "Use blue."},
                                        ],
                                    }
                                ]
                            }
                        ),
                    }
                ]
            },
            {"text": _CHILD_DONE},
        ],
        key=_CHILD_MODEL,
    )
    agent_name = upload_agent(client, bundle)
    parent_id = create_runner_bound_session(client, agent_name=agent_name, runner_id=runner_id)
    session_ids = [parent_id]
    try:
        response = client.post(
            f"/v1/sessions/{parent_id}/events",
            json={
                "type": "message",
                "data": {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Ask claude_code to ask me the color question.",
                        }
                    ],
                },
            },
        )
        response.raise_for_status()
        assert response.json().get("queued") is True, response.text

        def find_child() -> dict[str, Any] | None:
            response = client.get(f"/v1/sessions/{parent_id}/child_sessions")
            response.raise_for_status()
            return next(
                (
                    row
                    for row in response.json()["data"]
                    if row.get("sub_agent_name") == "claude_code"
                    or row.get("tool") == "claude_code"
                    or str(row.get("title", "")).startswith("claude_code:")
                ),
                None,
            )

        child_row = _wait_for(
            find_child,
            description="Codex to dispatch the Claude child",
            client=client,
            session_ids=session_ids,
        )
        child_id = child_row["id"]
        session_ids.append(child_id)
        assert child_id != parent_id
        assert _snapshot(client, parent_id)["harness"] == "codex-native"
        child_snapshot = _snapshot(client, child_id)
        assert child_snapshot["harness"] == "claude-native", child_snapshot
        assert child_snapshot["parent_session_id"] == parent_id

        def mirrored_question() -> dict[str, Any] | None:
            return next(
                (
                    event
                    for event in _snapshot(client, parent_id).get("pending_elicitations", [])
                    if event["params"].get("target_session_id") == child_id
                    and event["params"].get("ask_user_question")
                ),
                None,
            )

        event = _wait_for(
            mirrored_question,
            description="the real Claude question in the Codex parent snapshot",
            client=client,
            session_ids=session_ids,
        )
        elicitation_id = event["elicitation_id"]
        params = event["params"]
        assert params["policy_name"] == "claude_native_permission"
        assert elicitation_id.startswith("elicit_claude_")
        assert any(
            pending["elicitation_id"] == elicitation_id
            for pending in _snapshot(client, child_id)["pending_elicitations"]
        )
        questions = params["ask_user_question"]["questions"]
        assert len(questions) == 1, questions
        assert questions[0]["question"] == _QUESTION
        answer = f"custom-color-{uuid.uuid4().hex}"
        # Use the target from the parent's card, exactly as chatStore does.
        response = client.post(
            f"/v1/sessions/{params['target_session_id']}/elicitations/{elicitation_id}/resolve",
            json={"action": "accept", "content": {questions[0]["question"]: answer}},
        )
        assert response.status_code == 202, response.text

        _wait_for(
            lambda: any(
                block.get("type") == "tool_result"
                and block.get("tool_use_id") == "ask-color"
                and answer in json.dumps(block.get("content"))
                for body in get_mock_requests(mock_url, key=_CHILD_MODEL)
                for message in body.get("messages", [])
                for block in message.get("content", [])
                if isinstance(block, dict)
            ),
            description="Claude to send the actual answer back to the model as a tool result",
            client=client,
            session_ids=session_ids,
        )
        _wait_for(
            lambda: any(
                item.get("type") == "message"
                and item.get("role") == "assistant"
                and _CHILD_DONE in json.dumps(item.get("content"))
                for item in _items(client, child_id)
            ),
            description="Claude to finish its turn after the question",
            client=client,
            session_ids=session_ids,
        )
        _wait_for(
            lambda: all(
                not any(
                    pending["elicitation_id"] == elicitation_id
                    for pending in _snapshot(client, sid).get("pending_elicitations", [])
                )
                for sid in session_ids
            ),
            description="the resolved card to clear from both parent and child",
            client=client,
            session_ids=session_ids,
            timeout=30,
        )
    finally:
        for sid in reversed(session_ids):
            with contextlib.suppress(httpx.HTTPError):
                client.post(f"/v1/sessions/{sid}/events", json={"type": "stop_session"})
                client.delete(f"/v1/sessions/{sid}")
