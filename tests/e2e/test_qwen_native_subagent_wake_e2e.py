"""End-to-end: a qwen-native sub-agent's completed turn must wake its parent.

A parent orchestrator dispatches a qwen-native sub-agent via
``sys_session_send`` and goes idle. The child runs headlessly (the runner owns
its tmux pane), finishes its turn, and its answer is persisted to the child
transcript. The runner must then deliver the result to the parent's inbox and
post the ``[System: ... waiting in inbox]`` wake notice so the idle parent
takes a continuation turn and surfaces the result — the same contract the
claude-/codex-/cursor-/kimi-native forwarders honor via an
``external_session_status`` terminal edge.

The reported bug: the qwen-native forwarder mirrors
the child's transcript but never posts a terminal ``external_session_status``,
so a *successful* qwen child finishes silently — the parent sits on
"dispatched, waiting" forever and only a manual bump reveals the result. The
failure edge is unaffected (a child that dies at launch DOES wake the parent),
which this test exploits: it drives the SUCCESS path and asserts the wake.

Requires a runnable ``qwen`` CLI (skipped otherwise). Point
``OMNIGENT_QWEN_PATH`` at a raw qwen binary if the PATH one wraps/overrides
``OPENAI_BASE_URL`` (the child must reach this test's mock LLM).

Excluded from default ``pytest`` runs via ``--ignore=tests/e2e``. Invoke
with::

    pytest tests/e2e/test_qwen_native_subagent_wake_e2e.py -v --timeout=900
"""

from __future__ import annotations

import io
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
from tests._helpers.compat import apply_runner_env, apply_server_env
from tests.e2e.conftest import configure_mock_llm, reset_mock_llm

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The auto-wake notice is emitted ONLY by the runner's auto-wake path
# (``_format_subagent_wake_notice``); the sys_read_inbox drain message does not
# contain it, so its presence is an auto-wake-specific signal.
_WAKE_NOTICE_SIGNATURE = "waiting in inbox"
_CHILD_MARKER = "QWEN_SUBAGENT_DONE_MARKER"
_CONTINUATION_MARKER = "CONTINUATION_MARKER"

_PARENT_MODEL = "mock-qwen-wake-parent"
# The qwen TUI reads its model from OPENAI_MODEL/QWEN_MODEL in the runner env;
# this key routes the child's mock-LLM requests to its own queue.
_CHILD_MODEL = "mock-qwen-wake-child"

_HEALTH_TIMEOUT_S = 90.0
# qwen boots a real TUI in tmux, then runs one mock-LLM turn.
_CHILD_ANSWER_TIMEOUT_S = 300.0
# Once the child's answer is in its transcript, the wake should follow almost
# immediately (the forwarder tails the child's event file sub-second). A
# generous window avoids flaking on a slow box while still failing decisively.
_WAKE_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 2.0

_LOOPBACK_NO_PROXY = "localhost,127.0.0.1"

pytestmark = [pytest.mark.timeout(900, method="signal")]


def _qwen_cli() -> str | None:
    """Resolve the qwen binary the runner will launch, or ``None`` if absent."""
    override = os.environ.get("OMNIGENT_QWEN_PATH", "").strip()
    if override:
        return override if Path(override).exists() else None
    return shutil.which("qwen")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _ambient_free_environ() -> dict[str, str]:
    """``os.environ`` minus ambient runner/host identity vars (hermetic stack)."""
    return {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_HOST_"))
        and k
        not in (
            "RUNNER_SERVER_URL",
            "OMNIGENT_REMOTE_AUTH_TOKEN",
            # An inherited process-log path may point at another process's
            # (possibly unwritable) log dir and crash the spawned runner.
            "OMNIGENT_PROCESS_LOG_FILE",
        )
    }


def _merged_no_proxy(env: dict[str, str]) -> str:
    existing = env.get("NO_PROXY") or env.get("no_proxy") or ""
    parts = [p for p in existing.split(",") if p]
    for host in _LOOPBACK_NO_PROXY.split(","):
        if host not in parts:
            parts.append(host)
    return ",".join(parts)


# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars
# that must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False, timeout=30.0)


@pytest.fixture
def qwen_wake_rig(
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """Server + runner wired to the mock LLM, with qwen-native launchable.

    The runner env carries ``OPENAI_BASE_URL`` → mock LLM and
    ``OPENAI_MODEL``/``QWEN_MODEL`` → the child queue key, so the qwen TUI the
    runner launches for the sub-agent completes its turn against the mock.
    ``OMNIGENT_RUNNER_WORKSPACE`` is set because the runner-owned qwen terminal
    requires a workspace to launch headlessly.

    :returns: ``(base_url, runner_id, runner_log_path)``.
    """
    if _qwen_cli() is None:
        pytest.skip("qwen CLI is required for the qwen-native sub-agent wake repro")

    work = tmp_path_factory.mktemp("qwen_subagent_wake")
    artifacts = work / "artifacts"
    workspace = work / "ws"
    home_dir = work / "home"
    for path in (artifacts, workspace, home_dir):
        path.mkdir(parents=True, exist_ok=True)

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    shared_env = _ambient_free_environ()
    shared_env.update(
        {
            "OPENAI_API_KEY": "mock-key",
            "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        }
    )
    shared_env["NO_PROXY"] = _merged_no_proxy(shared_env)
    shared_env["no_proxy"] = shared_env["NO_PROXY"]

    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    apply_server_env(server_env, _REPO_ROOT)

    runner_env = apply_runner_env(
        {
            **shared_env,
            "OMNIGENT_RUNNER_ID": runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": base_url,
            "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
            # HOME is redirected so an ambient ~/.qwen/settings.json can't
            # override the env-configured mock auth/model (qwen prefers its
            # user settings over env when both exist).
            "HOME": str(home_dir),
            # The qwen TUI the runner spawns inherits these; they pin its model
            # to the child's mock queue.
            "OPENAI_MODEL": _CHILD_MODEL,
            "QWEN_MODEL": _CHILD_MODEL,
            "QWEN_CODE_DISABLE_UPDATE_CHECK": "1",
        }
    )
    qwen_path = _qwen_cli()
    assert qwen_path is not None  # guarded by the skip above
    runner_env["OMNIGENT_QWEN_PATH"] = qwen_path
    # PYTHONPATH must also resolve the workspace SDKs (omnigent_client) for the
    # runner's tunnel bootstrap; extend rather than replace what apply_server_env
    # composed for the server.
    sdk_path = str(_REPO_ROOT / "sdks" / "python-client")
    existing_pp = runner_env.get("PYTHONPATH") or server_env.get("PYTHONPATH", "")
    runner_env["PYTHONPATH"] = os.pathsep.join(
        p for p in (str(_REPO_ROOT), sdk_path, existing_pp) if p
    )

    server_log = work / "server.log"
    runner_log = work / "runner.log"
    server_handle = server_log.open("w")
    runner_handle = runner_log.open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    try:
        server_proc = subprocess.Popen(
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
                f"sqlite:///{work}/test.db",
                "--artifact-location",
                str(artifacts),
            ],
            env=server_env,
            stdout=server_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            if server_proc.poll() is not None or runner_proc.poll() is not None:
                break
            try:
                if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                    status = _client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online"):
                        online = True
                        break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        if not online:
            raise RuntimeError(
                "qwen sub-agent wake rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )
        yield (base_url, runner_id, runner_log)
    finally:
        for proc in (runner_proc, server_proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in (runner_proc, server_proc):
            if proc is not None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        server_handle.close()
        runner_handle.close()


def _parent_config() -> dict:
    """Orchestrator spec (dict) with a qwen-native sub-agent tool."""
    return {
        "name": "qwen-wake-parent",
        "prompt": (
            "You are an orchestrator. Dispatch the qwencoder sub-agent via "
            "sys_session_send when asked, and report its result when woken."
        ),
        "executor": {
            "harness": "openai-agents",
            "model": _PARENT_MODEL,
            "auth": {
                "type": "api_key",
                "api_key": "mock-key",
                # Placeholder; rewritten per-rig in _register_parent.
                "base_url": "http://mock",
            },
        },
        "tools": {
            "qwencoder": {
                "type": "agent",
                "description": "Qwen coding sub-agent (qwen-native harness).",
                "executor": {"harness": "qwen-native"},
                "prompt": "You are a qwen coding sub-agent.",
            }
        },
    }


def _register_parent(base_url: str, mock_llm_server_url: str) -> str:
    """Upload the orchestrator agent and return its durable agent id."""
    config = _parent_config()
    config["executor"]["auth"]["base_url"] = f"{mock_llm_server_url}/v1"
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml.dump(config).encode()
        info = tarfile.TarInfo("qwen-wake-parent.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    resp = _client.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    if resp.status_code not in (200, 201, 409):
        raise RuntimeError(f"agent register failed: {resp.status_code} {resp.text[:500]}")
    listing = _client.get(
        f"{base_url}/v1/sessions", params={"agent_name": "qwen-wake-parent", "limit": 1}
    )
    listing.raise_for_status()
    return str(listing.json()["data"][0]["agent_id"])


def _configure_queues(mock_llm_server_url: str) -> None:
    """Queue the parent's dispatch → ack → continuation and the child's answer."""
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_1",
                        "name": "sys_session_send",
                        "arguments": json.dumps(
                            {
                                "agent": "qwencoder",
                                "title": "qwen-task",
                                "args": "Say the completion marker.",
                            }
                        ),
                    }
                ],
            },
            {"text": "Dispatched the qwen sub-agent, waiting for its result."},
            # Only reachable via the auto-wake continuation turn.
            {"text": f"{_CONTINUATION_MARKER}: the qwen sub-agent returned {_CHILD_MARKER}"},
        ],
        key=_PARENT_MODEL,
    )
    # qwen issues several chat-completions calls per turn (main + auxiliary
    # extractors); keep the queue deep so every call sees the marker text.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": f"Task complete. {_CHILD_MARKER}"}] * 8,
        key=_CHILD_MODEL,
    )


def _session_items_blob(base_url: str, session_id: str) -> str:
    resp = _client.get(f"{base_url}/v1/sessions/{session_id}")
    resp.raise_for_status()
    return json.dumps(resp.json().get("items", []))


def _child_id_from_dispatch(base_url: str, session_id: str) -> str | None:
    """Pull the child conversation id out of the sys_session_send tool output."""
    resp = _client.get(f"{base_url}/v1/sessions/{session_id}")
    resp.raise_for_status()
    for item in resp.json().get("items", []):
        data = item.get("data") or {}
        output = data.get("output") or item.get("output") or ""
        if isinstance(output, str) and "conversation_id" in output:
            try:
                return str(json.loads(output)["conversation_id"])
            except (ValueError, KeyError):
                continue
    return None


def test_qwen_native_subagent_completion_wakes_parent(
    qwen_wake_rig: tuple[str, str, Path],
    mock_llm_server_url: str,
) -> None:
    """A completed qwen-native sub-agent turn must auto-wake the idle parent.

    Journey:
    1. The user asks the orchestrator to dispatch its qwen-native sub-agent.
    2. The dispatch turn ends; the child runs headlessly and finishes its turn
       (its answer is persisted to the child transcript — observed here).
    3. With NO further user input, the parent must receive the auto-wake
       notice and take the continuation turn that surfaces the result.

    While the bug is live, step 3 never happens for a SUCCESSFUL child (the
    qwen forwarder posts no terminal ``external_session_status``), so the wake
    assertion fails after proving the child had answered.
    """
    base_url, runner_id, runner_log = qwen_wake_rig
    _configure_queues(mock_llm_server_url)

    agent_id = _register_parent(base_url, mock_llm_server_url)
    create = _client.post(
        f"{base_url}/v1/sessions",
        json={"agent_id": agent_id},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    create.raise_for_status()
    session_id = str(create.json()["id"])
    bind = _client.patch(f"{base_url}/v1/sessions/{session_id}", json={"runner_id": runner_id})
    bind.raise_for_status()

    send = _client.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": "Dispatch the qwencoder sub-agent."}],
            },
        },
    )
    assert send.status_code == 202, f"send rejected: {send.status_code} {send.text}"

    # ── Step 2: wait for the child to be minted and ANSWER (success path). ──
    child_id: str | None = None
    child_answered = False
    child_error: str | None = None
    deadline = time.monotonic() + _CHILD_ANSWER_TIMEOUT_S
    while time.monotonic() < deadline:
        if child_id is None:
            child_id = _child_id_from_dispatch(base_url, session_id)
        if child_id is not None:
            child_snap = _client.get(f"{base_url}/v1/sessions/{child_id}")
            if child_snap.status_code == 200:
                child_body = child_snap.json()
                child_blob = json.dumps(child_body.get("items", []))
                if _CHILD_MARKER in child_blob:
                    child_answered = True
                    break
                if child_body.get("status") == "failed":
                    child_error = json.dumps(child_body.get("last_task_error"))
                    break
        time.sleep(_POLL_INTERVAL_S)

    assert child_error is None, (
        "qwen-native child failed to launch/run — this exercises the (working) "
        f"failure-wake edge, not the bug under test: {child_error}\n"
        f"runner log tail:\n{runner_log.read_text()[-2000:]}"
    )
    assert child_answered, (
        f"qwen-native child (session {child_id}) never persisted its answer within "
        f"{_CHILD_ANSWER_TIMEOUT_S:.0f}s — cannot reach the completion-wake edge.\n"
        f"runner log tail:\n{runner_log.read_text()[-2000:]}"
    )

    # ── Step 3: the completion must wake the parent with NO further input. ──
    wake_seen = False
    continuation_seen = False
    deadline = time.monotonic() + _WAKE_TIMEOUT_S
    while time.monotonic() < deadline:
        blob = _session_items_blob(base_url, session_id)
        wake_seen = wake_seen or (_WAKE_NOTICE_SIGNATURE in blob)
        continuation_seen = continuation_seen or (_CONTINUATION_MARKER in blob)
        if wake_seen and continuation_seen:
            break
        time.sleep(_POLL_INTERVAL_S)

    assert wake_seen, (
        f"qwen-native sub-agent {child_id} completed its turn (its answer "
        f"{_CHILD_MARKER!r} is persisted in the child transcript) but the parent "
        f"session {session_id} never received the auto-wake notice "
        f"({_WAKE_NOTICE_SIGNATURE!r}) within {_WAKE_TIMEOUT_S:.0f}s — the "
        "completion never woke the parent orchestrator. "
        "claude-/codex-native children wake the parent on the same journey."
    )
    assert continuation_seen, (
        f"The wake notice arrived but the parent never took the continuation turn "
        f"surfacing {_CONTINUATION_MARKER!r} in session {session_id}."
    )
