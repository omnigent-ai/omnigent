"""Real native sessions forward a cross-harness elicitation with mock models.

Codex creates a Claude child through MCP. Claude calls AskUserQuestion via
its real permission hook, and the parent snapshot exposes the child's card.
The answer must reach Claude's next Messages API request as a tool result.

The model APIs are mocked; the server, runner, native CLIs, and hooks are real.
The flow runs with clean and conflicting dummy provider environments.
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
from types import SimpleNamespace
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
def cross_harness_deadline() -> float:
    """Reserve two minutes of the outer timeout for diagnostics and teardown."""
    return time.monotonic() + 240


@pytest.fixture(params=["clean", "bedrock", "vertex", "foundry"])
def ambient_provider_env(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Exercise real startup with conflicting, loopback-only provider settings."""
    if request.param == "clean":
        return
    provider_env = {
        "bedrock": {
            "CLAUDE_CODE_USE_BEDROCK": "1",
            "ANTHROPIC_BEDROCK_BASE_URL": "http://127.0.0.1:1/bedrock",
            "AWS_ENDPOINT_URL": "http://127.0.0.1:1/aws",
            "AWS_ACCESS_KEY_ID": "dummy-access-key",
            "AWS_SECRET_ACCESS_KEY": "dummy-secret-key",
            "AWS_SESSION_TOKEN": "dummy-session-token",
            "AWS_BEARER_TOKEN_BEDROCK": "dummy-bedrock-token",
            "AWS_REGION": "us-east-1",
            "AWS_EC2_METADATA_DISABLED": "true",
        },
        "vertex": {
            "CLAUDE_CODE_USE_VERTEX": "1",
            "ANTHROPIC_VERTEX_BASE_URL": "http://127.0.0.1:1/vertex",
            "ANTHROPIC_VERTEX_PROJECT_ID": "dummy-project",
            "CLOUD_ML_REGION": "us-east5",
            "GOOGLE_APPLICATION_CREDENTIALS": str(tmp_path / "no-google-credentials.json"),
        },
        "foundry": {
            "CLAUDE_CODE_USE_FOUNDRY": "1",
            "ANTHROPIC_FOUNDRY_BASE_URL": "http://127.0.0.1:1/foundry",
            "ANTHROPIC_FOUNDRY_API_KEY": "dummy-foundry-key",
            "AZURE_CLIENT_ID": "dummy-client-id",
            "AZURE_CLIENT_SECRET": "dummy-client-secret",
            "AZURE_TENANT_ID": "dummy-tenant-id",
        },
    }[request.param]
    for key, value in {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:1/anthropic",
        "ANTHROPIC_AUTH_TOKEN": "dummy-anthropic-token",
        "ANTHROPIC_API_KEY": "dummy-anthropic-key",
        "OPENAI_BASE_URL": "http://127.0.0.1:1/openai",
        "OPENAI_API_KEY": "dummy-openai-key",
        **provider_env,
    }.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def cross_harness_rig(
    cross_harness_deadline: float,
    ambient_provider_env: None,
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
    # Seed before any Claude process (including model probes) can cache
    # an incomplete config and later overwrite the runner's onboarding state.
    (native_home / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "projects": {str(workspace.resolve()): {"hasTrustDialogAccepted": True}},
            }
        ),
        encoding="utf-8",
    )
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
        **{
            key: value
            for key, value in os.environ.items()
            if key
            in {
                "PATH",
                "LANG",
                "LC_ALL",
                "SYSTEMROOT",
                "WINDIR",
                "TMPDIR",
                "TMP",
                "TEMP",
                "SSL_CERT_FILE",
                "SSL_CERT_DIR",
                "REQUESTS_CA_BUNDLE",
                "NODE_EXTRA_CA_CERTS",
            }
        },
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
            deadline = min(cross_harness_deadline, time.monotonic() + 60)
            while time.monotonic() < deadline:
                if any(proc.poll() is not None for proc in processes):
                    pytest.fail(f"server/runner exited early:\n{_rig_log_tails(tmp_path)}")
                with contextlib.suppress(httpx.HTTPError):
                    response = client.get(f"/v1/runners/{runner_id}/status", timeout=2)
                    if response.status_code == 200 and response.json().get("online"):
                        break
                time.sleep(0.5)
            else:
                pytest.fail(f"server/runner did not become ready:\n{_rig_log_tails(tmp_path)}")
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


def _rig_log_tails(tmp_path: Path) -> str:
    """Tail of the spawned server/runner logs, for a stall's diagnostics."""
    tails = []
    for name in ("server.log", "runner.log"):
        path = tmp_path / name
        try:
            tails.append(f"{name}:\n{path.read_text()[-3000:]}")
        except OSError as exc:
            tails.append(f"{name}: unavailable ({exc})")
    return "\n".join(tails)


def _snapshot(client: httpx.Client, session_id: str) -> dict[str, Any]:
    response = client.get(f"/v1/sessions/{session_id}", timeout=5)
    response.raise_for_status()
    return response.json()


def _items(client: httpx.Client, session_id: str) -> list[dict[str, Any]]:
    response = client.get(
        f"/v1/sessions/{session_id}/items", params={"order": "asc", "limit": 1000}, timeout=5
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
    deadline: float,
    timeout: float | None = None,
    tmp_path: Path | None = None,
) -> Any:
    """Poll real processes and retain session diagnostics on timeout.

    :param tmp_path: When given, the failure message also includes the
        spawned server/runner log tails — a crashed or wedged native CLI
        subprocess shows up there, not in a session snapshot.
    """
    if timeout is not None:
        deadline = min(deadline, time.monotonic() + timeout)
    while time.monotonic() < deadline:
        result = probe()
        if result:
            return result
        time.sleep(min(1, max(0, deadline - time.monotonic())))
    diagnostics = {}
    for session_id in session_ids:
        try:
            diagnostics[session_id] = {
                "snapshot": _snapshot(client, session_id),
                "items": _items(client, session_id),
            }
        except httpx.HTTPError as exc:
            diagnostics[session_id] = {"error": str(exc)}
    message = f"Timed out waiting for {description}: {json.dumps(diagnostics, default=str)}"
    if tmp_path is not None:
        message += f"\n{_rig_log_tails(tmp_path)}"
    pytest.fail(message)


@pytest.mark.parametrize("diagnostics_unavailable", [False, True])
def test_waits_share_deadline_and_collect_diagnostics(
    monkeypatch: pytest.MonkeyPatch,
    diagnostics_unavailable: bool,
) -> None:
    """A late stall reports session diagnostics before the outer timeout."""
    clock = SimpleNamespace(now=0.0)

    def advance(seconds: float) -> None:
        clock.now += seconds

    monkeypatch.setattr(
        sys.modules[__name__],
        "time",
        SimpleNamespace(monotonic=lambda: clock.now, sleep=advance),
    )

    def response(request: httpx.Request) -> httpx.Response:
        if diagnostics_unavailable:
            return httpx.Response(503)
        if request.url.path.endswith("/items"):
            return httpx.Response(200, json={"data": [{"content": "last child message"}]})
        return httpx.Response(200, json={"pending_elicitations": ["pending-question"]})

    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(response)) as client:
        deadline = 5.0
        assert _wait_for(
            lambda: clock.now >= 3,
            description="an earlier step",
            client=client,
            session_ids=["child"],
            deadline=deadline,
        )
        with pytest.raises(
            pytest.fail.Exception, match="Timed out waiting for a late stall"
        ) as failure:
            _wait_for(
                lambda: False,
                description="a late stall",
                client=client,
                session_ids=["child"],
                deadline=deadline,
            )
    assert clock.now == deadline
    if diagnostics_unavailable:
        assert "503" in str(failure.value)
    else:
        assert "pending-question" in str(failure.value)
        assert "last child message" in str(failure.value)


def test_codex_parent_answers_real_claude_child_elicitation(
    cross_harness_deadline: float,
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
            deadline=cross_harness_deadline,
            client=client,
            session_ids=session_ids,
            tmp_path=tmp_path,
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
            deadline=cross_harness_deadline,
            client=client,
            session_ids=session_ids,
            tmp_path=tmp_path,
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
            deadline=cross_harness_deadline,
            client=client,
            session_ids=session_ids,
            tmp_path=tmp_path,
        )
        _wait_for(
            lambda: any(
                item.get("type") == "message"
                and item.get("role") == "assistant"
                and _CHILD_DONE in json.dumps(item.get("content"))
                for item in _items(client, child_id)
            ),
            description="Claude to finish its turn after the question",
            deadline=cross_harness_deadline,
            client=client,
            session_ids=session_ids,
            tmp_path=tmp_path,
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
            deadline=cross_harness_deadline,
            client=client,
            session_ids=session_ids,
            timeout=30,
            tmp_path=tmp_path,
        )
    finally:
        for sid in reversed(session_ids):
            with contextlib.suppress(httpx.HTTPError):
                client.post(f"/v1/sessions/{sid}/events", json={"type": "stop_session"}, timeout=2)
                client.delete(f"/v1/sessions/{sid}", timeout=2)
