"""A sub-agent's later replies reach its parent.

A parent dispatches a worker with ``sys_session_send``. The worker's first reply reaches
the parent's inbox, wakes the parent, and the parent reads it with ``sys_read_inbox``.
Each test then makes the worker reply again without a new ``sys_session_send`` and
asserts that the parent is woken again and reads the later reply:

* a user message to the worker, after or before the parent read the first reply;
* a timer the worker set;
* a session message from another session, whose reply must not reach the parent,
  followed by a user message, whose reply must;
* the worker's own helper finishing;
* Claude Code continuing on its own after a background agent finishes.

The server, runner, harness CLIs and SDKs are real; only the model APIs are scripted.
Claude-based workers skip on machines whose managed Claude settings override the mock
provider, so run them in CI or a clean container. Run without credentials::

    uv run --no-sync pytest -o addopts='' tests/e2e/test_subagent_later_replies_e2e.py -v
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
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from omnigent.onboarding.harness_install import missing_harness_cli
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    find_free_port,
    release_mock_gate,
    set_fallback_mock_llm,
    upload_agent,
)

pytestmark = pytest.mark.timeout(360, method="signal")

_REPO = Path(__file__).resolve().parents[2]
_PROVIDER = "later-reply-models"
_PI_PROVIDER = "later-reply-pi"
# Claude Code refuses unknown model ids before sending a request, so Claude workers
# use a real id; the per-test mock server keeps queues isolated anyway.
_CLAUDE_MODEL = "claude-sonnet-4-6"
# pi only runs models its provider declares; replies route by marker, not model.
_OPENAI_PROVIDER_MODEL = "mock-later-reply-openai"
_WAKE_NOTICE = "waiting in inbox"
_CLAUDE_HARNESSES = frozenset({"claude-sdk", "claude-native"})
_CODEX_HARNESSES = frozenset({"codex", "codex-native"})
_NATIVE_HARNESSES = frozenset({"claude-native", "codex-native"})
# A machine-wide Codex config pins its own model provider, so Codex never calls the mock.
_SYSTEM_CODEX_CONFIG = Path("/etc/codex/managed_config.toml")
# Requests of a harness's real agent loop carry these tools; side requests such as
# title generation do not, so they cannot consume a worker's scripted replies.
# Every agent gets sys_session_list, which claude-sdk exposes under its MCP prefix.
_LOOP_TOOLS = {
    "claude-sdk": ["mcp__omnigent__sys_session_list"],
    "claude-native": ["Bash"],
    "pi": ["read"],
    "codex-native": ["mcp__omnigent"],
}


@dataclass(frozen=True)
class _Rig:
    client: httpx.Client
    runner_id: str
    mock_url: str
    bundles: Path


@dataclass(frozen=True)
class _Job:
    parent_id: str
    worker_id: str
    worker_harness: str
    uid: str


def _require_harness(harness: str) -> None:
    """Skip when a harness cannot run against the mock on this machine."""
    if harness in _NATIVE_HARNESSES and shutil.which("tmux") is None:
        pytest.skip("requires tmux")
    # The same check the runner applies before it dispatches a sub-agent.
    if (missing := missing_harness_cli(harness)) is not None:
        pytest.skip(f"requires a supported {missing.binary!r} CLI on PATH")
    if harness in _CLAUDE_HARNESSES and any(
        path.is_file() for path in CLAUDE_CODE_MANAGED_SETTINGS_PATHS
    ):
        pytest.skip("machine-managed Claude settings override mock auth; run in a clean container")
    if harness in _CODEX_HARNESSES and _SYSTEM_CODEX_CONFIG.exists():
        pytest.skip(
            "machine-managed Codex config overrides the mock provider; run in a clean container"
        )


@pytest.fixture
def rig(isolated_mock_llm_server_url: str, tmp_path: Path) -> Iterator[_Rig]:
    """Run an isolated server and runner whose harnesses all call the mock model API."""
    from omnigent.runner.identity import token_bound_runner_id

    mock_url = isolated_mock_llm_server_url
    workspace = tmp_path / "workspace"
    config_dir = tmp_path / "config"
    home = tmp_path / "home"
    for directory in (workspace, config_dir, home / ".codex", home / ".claude"):
        directory.mkdir(parents=True)
    # Seed before any Claude process can cache an incomplete onboarding state.
    (home / ".claude.json").write_text(
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
                    _PROVIDER: {
                        "kind": "key",
                        "default": ["openai", "anthropic"],
                        "openai": {
                            "base_url": f"{mock_url}/v1",
                            "api_key": "mock-key",
                            "wire_api": "responses",
                            "models": {"default": _OPENAI_PROVIDER_MODEL},
                        },
                        "anthropic": {
                            "base_url": mock_url,
                            "api_key": "mock-key",
                            "models": {"default": _CLAUDE_MODEL},
                        },
                    },
                    # pi takes its provider from the "pi" default scope, as Polly's pi
                    # worker does, so its spec names no auth.
                    _PI_PROVIDER: {
                        "kind": "key",
                        "default": ["pi"],
                        "openai": {
                            "base_url": f"{mock_url}/v1",
                            "api_key": "mock-key",
                            "wire_api": "chat",
                            "models": {"default": _OPENAI_PROVIDER_MODEL},
                        },
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    set_fallback_mock_llm(mock_url, key="_policy_llm_", text='{"action":"allow","reason":""}')
    set_fallback_mock_llm(mock_url, key=_CLAUDE_MODEL, text="Acknowledged.")
    for marker in ("<session>", "<user_message>", "Generate a concise, single-line task title"):
        configure_mock_llm(mock_url, [{"text": "Working."}] * 8, match=marker)

    token = uuid.uuid4().hex
    runner_id = token_bound_runner_id(token)
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    kept = {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    env = {
        **{key: value for key, value in os.environ.items() if key in kept},
        "HOME": str(home),
        "CODEX_HOME": str(home / ".codex"),
        "OMNIGENT_CONFIG_HOME": str(config_dir),
        "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
        "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
        "OMNIGENT_SKIP_WEB_UI": "true",
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        "PYTHONPATH": str(_REPO),
    }
    for name, binary in (("CODEX", "codex"), ("CLAUDE", "claude")):
        if path := shutil.which(binary):
            env[f"OMNIGENT_{name}_PATH"] = path
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
            bundles = tmp_path / "bundles"
            bundles.mkdir()
            yield _Rig(client=client, runner_id=runner_id, mock_url=mock_url, bundles=bundles)
        finally:
            for proc in reversed(processes):
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=5)


# ─── Specs and scripts ───────────────────────────────────────


def _model(harness: str, role: str, uid: str) -> str:
    """Return a model id the harness accepts; Claude and pi need a known one."""
    if harness in _CLAUDE_HARNESSES:
        return _CLAUDE_MODEL
    return _OPENAI_PROVIDER_MODEL if harness == "pi" else f"mock-{role}-{uid}"


def _agent_spec(
    rig: _Rig, *, name: str, harness: str, model: str, prompt: str, **extra: Any
) -> dict[str, Any]:
    """Build an agent spec that sends this harness's model calls to the mock."""
    config: dict[str, Any] = {"harness": harness}
    if harness == "codex-native":
        config["yolo"] = True
    elif harness == "claude-native":
        # No permission prompt may hold a scripted turn, e.g. for the Agent tool.
        config["permission_mode"] = "bypassPermissions"
    executor: dict[str, Any] = {"type": "omnigent", "config": config}
    if harness in _NATIVE_HARNESSES:
        executor["auth"] = {"type": "provider", "name": _PROVIDER}
    elif harness != "pi":
        base_url = rig.mock_url if harness == "claude-sdk" else f"{rig.mock_url}/v1"
        executor["auth"] = {"type": "api_key", "api_key": "mock-key", "base_url": base_url}
    return {
        "spec_version": 1,
        "name": name,
        "executor": executor,
        "llm": {"model": model},
        "prompt": prompt,
        "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
        **extra,
    }


def _tool_call(harness: str, name: str, arguments: dict[str, Any], call_id: str) -> dict[str, Any]:
    """Script one tool call in the shape this harness's model API returns."""
    if harness in _CLAUDE_HARNESSES and name.startswith("sys_"):
        # Claude reaches Omnigent's tools through the MCP bridge's prefix.
        name = f"mcp__omnigent__{name}"
    if harness == "codex-native":
        return {
            "text": "",
            "native_items": [
                {
                    "type": "function_call",
                    "id": f"fc-{call_id}",
                    "call_id": call_id,
                    "namespace": "mcp__omnigent",
                    "name": name,
                    "arguments": json.dumps(arguments),
                }
            ],
        }
    return {"tool_calls": [{"call_id": call_id, "name": name, "arguments": json.dumps(arguments)}]}


def _script_worker(
    rig: _Rig, harness: str, token: str, replies: list[dict[str, Any]], **routing: Any
) -> None:
    """Queue a worker's replies for the turn whose user text carries ``token``."""
    configure_mock_llm(
        rig.mock_url,
        replies,
        match=token,
        required_tools=_LOOP_TOOLS.get(harness),
        **routing,
    )


def _inbox_reads(rig: _Rig, session_id: str) -> str:
    """Return everything the session's ``sys_read_inbox`` calls returned."""
    calls = {
        item.get("call_id")
        for item in _items(rig, session_id)
        if item.get("type") == "function_call" and item.get("name") == "sys_read_inbox"
    }
    return "\n".join(
        str(item.get("output"))
        for item in _items(rig, session_id)
        if item.get("type") == "function_call_output" and item.get("call_id") in calls
    )


def _items(rig: _Rig, session_id: str) -> list[dict[str, Any]]:
    response = rig.client.get(
        f"/v1/sessions/{session_id}/items", params={"order": "asc", "limit": 1000}, timeout=10
    )
    response.raise_for_status()
    return [
        {**item, **item["data"]} if isinstance(item.get("data"), dict) else item
        for item in response.json()["data"]
    ]


def _text(rig: _Rig, session_id: str) -> str:
    return json.dumps(_items(rig, session_id))


def _send(rig: _Rig, session_id: str, text: str) -> None:
    """Post a user message, as a person typing into the session does."""
    response = rig.client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
        },
    )
    assert response.status_code == 202, response.text


def _wait_for(probe: Callable[[], Any], *, description: str, timeout_s: float = 150) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if result := probe():
            return result
        time.sleep(1.0)
    raise AssertionError(f"Timed out after {timeout_s:.0f}s waiting for {description}")


def _start_job(
    rig: _Rig,
    worker_harness: str,
    *,
    uid: str,
    first_turn: list[dict[str, Any]],
    worker_extra: dict[str, Any] | None = None,
    worker_agents: dict[str, dict[str, Any]] | None = None,
    hold_first_read: bool = False,
) -> _Job:
    """Dispatch a worker whose first turn plays ``first_turn``, then wait for it to land.

    The parent reads its inbox on every wake. With ``hold_first_read`` the parent's
    first read is parked on the mock gate until the test releases it.
    ``worker_agents`` maps the worker's own sub-agent names to their specs.
    """
    _require_harness(worker_harness)
    parent_model = f"mock-parent-{uid}"
    parent_name = f"later-reply-parent-{uid}"
    bundle = rig.bundles / parent_name
    worker_dir = bundle / "agents" / "worker"
    worker_dir.mkdir(parents=True)
    parent = _agent_spec(
        rig,
        name=parent_name,
        harness="openai-agents",
        model=parent_model,
        prompt="Dispatch the worker with sys_session_send; read your inbox when woken.",
        tools={"agents": ["worker"]},
        **{"async": True},
    )
    worker_model = _model(worker_harness, "worker", uid)
    worker_extra = dict(worker_extra or {})
    if worker_agents:
        worker_extra["tools"] = {"agents": sorted(worker_agents)}
    worker = _agent_spec(
        rig,
        name="worker",
        harness=worker_harness,
        model=worker_model,
        prompt="You are the worker. Answer each request in one short reply.",
        **worker_extra,
    )
    (bundle / "config.yaml").write_text(yaml.safe_dump(parent), encoding="utf-8")
    (worker_dir / "config.yaml").write_text(yaml.safe_dump(worker), encoding="utf-8")
    for agent_name, spec in (worker_agents or {}).items():
        agent_dir = worker_dir / "agents" / agent_name
        agent_dir.mkdir(parents=True)
        (agent_dir / "config.yaml").write_text(yaml.safe_dump(spec), encoding="utf-8")

    read = _tool_call("openai-agents", "sys_read_inbox", {}, "read-1")
    configure_mock_llm(
        rig.mock_url,
        [
            _tool_call(
                "openai-agents",
                "sys_session_send",
                {"agent": "worker", "title": "job", "args": f"dispatch-{uid}: do the job"},
                "dispatch",
            ),
            {"text": "Dispatched the worker."},
            {**read, "block": True} if hold_first_read else read,
            {"text": "Read the worker's reply."},
            *[
                item
                for wake in range(2, 6)
                for item in (
                    _tool_call("openai-agents", "sys_read_inbox", {}, f"read-{wake}"),
                    {"text": "Read the worker's later reply."},
                )
            ],
        ],
        key=parent_model,
    )
    set_fallback_mock_llm(rig.mock_url, key=parent_model, text="Noted.")
    set_fallback_mock_llm(rig.mock_url, key=worker_model, text="Working.")
    _script_worker(rig, worker_harness, f"dispatch-{uid}", first_turn)

    agent_name = upload_agent(rig.client, bundle)
    parent_id = create_runner_bound_session(
        rig.client, agent_name=agent_name, runner_id=rig.runner_id
    )
    _send(rig, parent_id, "Dispatch the worker.")

    def find_worker() -> str | None:
        response = rig.client.get(f"/v1/sessions/{parent_id}/child_sessions")
        response.raise_for_status()
        rows = response.json()["data"]
        return rows[0]["id"] if rows else None

    try:
        worker_id = _wait_for(find_worker, description="the parent to dispatch the worker")
    except AssertionError as exc:
        raise AssertionError(
            f"{exc}; parent tool outputs: {_tool_outputs(rig, parent_id)}"
        ) from None
    return _Job(parent_id=parent_id, worker_id=worker_id, worker_harness=worker_harness, uid=uid)


def _tool_outputs(rig: _Rig, session_id: str) -> list[str]:
    """Return the session's tool results, for failure messages."""
    return [
        str(item.get("output"))[:500]
        for item in _items(rig, session_id)
        if item.get("type") == "function_call_output"
    ]


def _assert_parent_reads(rig: _Rig, job: _Job, marker: str, *, absent: str | None = None) -> None:
    """The parent must be woken again and read ``marker`` from its inbox."""
    _wait_for(
        lambda: marker in _text(rig, job.worker_id),
        description=f"the {job.worker_harness} worker to reply {marker!r}",
    )
    try:
        # Delivery follows the reply within seconds; the slack absorbs CI load.
        _wait_for(
            lambda: marker in _inbox_reads(rig, job.parent_id),
            description=f"the parent to read {marker!r} from its inbox",
            timeout_s=60,
        )
    except AssertionError as exc:
        wakes = _text(rig, job.parent_id).count(_WAKE_NOTICE)
        raise AssertionError(
            f"The {job.worker_harness} worker {job.worker_id} replied {marker!r}, but its "
            f"parent {job.parent_id} never read it (parent wake notices: {wakes}). {exc}"
        ) from None
    if absent is not None:
        assert absent not in _inbox_reads(rig, job.parent_id), (
            f"The parent read {absent!r}, the worker's reply to another session"
        )


def _wait_first_read(rig: _Rig, job: _Job) -> None:
    try:
        _wait_for(
            lambda: f"FIRST-{job.uid}" in _inbox_reads(rig, job.parent_id),
            description="the parent to read the worker's first reply",
        )
    except AssertionError as exc:
        transcript = _text(rig, job.worker_id)[-1500:]
        raise AssertionError(
            f"{exc}; {job.worker_harness} worker transcript tail: {transcript}"
        ) from None


# ─── Tests ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    "worker_harness",
    ["openai-agents", "claude-sdk", "codex", "pi", "claude-native", "codex-native"],
)
def test_reply_to_a_user_message_reaches_the_parent(rig: _Rig, worker_harness: str) -> None:
    """A person messages the worker after the parent read its first reply."""
    uid = uuid.uuid4().hex[:8]
    job = _start_job(rig, worker_harness, uid=uid, first_turn=[{"text": f"FIRST-{uid}"}])
    _wait_first_read(rig, job)
    _script_worker(rig, worker_harness, f"user-follow-up-{uid}", [{"text": f"SECOND-{uid}"}])
    _send(rig, job.worker_id, f"user-follow-up-{uid}: one more thing")
    _assert_parent_reads(rig, job, f"SECOND-{uid}")


@pytest.mark.parametrize("worker_harness", ["openai-agents", "claude-sdk", "claude-native"])
def test_reply_before_the_parent_reads_the_first_reaches_the_parent(
    rig: _Rig, worker_harness: str
) -> None:
    """The worker replies again while its first reply still sits unread in the inbox."""
    uid = uuid.uuid4().hex[:8]
    job = _start_job(
        rig,
        worker_harness,
        uid=uid,
        first_turn=[{"text": f"FIRST-{uid}"}],
        hold_first_read=True,
    )
    _wait_for(
        lambda: _WAKE_NOTICE in _text(rig, job.parent_id),
        description="the worker's first reply to wake the parent",
    )
    _script_worker(rig, worker_harness, f"user-follow-up-{uid}", [{"text": f"SECOND-{uid}"}])
    _send(rig, job.worker_id, f"user-follow-up-{uid}: one more thing")
    _wait_for(
        lambda: f"SECOND-{uid}" in _text(rig, job.worker_id),
        description="the worker's second reply",
    )
    release_mock_gate(rig.mock_url)
    _assert_parent_reads(rig, job, f"SECOND-{uid}")
    assert f"FIRST-{uid}" in _inbox_reads(rig, job.parent_id)


@pytest.mark.parametrize("worker_harness", ["openai-agents", "claude-sdk"])
def test_reply_after_the_worker_timer_fires_reaches_the_parent(
    rig: _Rig, worker_harness: str
) -> None:
    """The worker sets a timer in its first turn and replies again when it fires."""
    uid = uuid.uuid4().hex[:8]
    job = _start_job(
        rig,
        worker_harness,
        uid=uid,
        first_turn=[
            _tool_call(
                worker_harness,
                "sys_timer_set",
                {"seconds": 5, "note": f"timer-check-back-{uid}"},
                "set-timer",
            ),
            {"text": f"FIRST-{uid}"},
        ],
        worker_extra={"timers": True},
    )
    _script_worker(rig, worker_harness, f"timer-check-back-{uid}", [{"text": f"SECOND-{uid}"}])
    _assert_parent_reads(rig, job, f"SECOND-{uid}")


@pytest.mark.parametrize("worker_harness", ["openai-agents", "claude-sdk"])
def test_reply_to_another_session_stays_out_of_the_parent_inbox(
    rig: _Rig, worker_harness: str
) -> None:
    """Another session messages the worker; only the next user message's reply reaches
    the parent, because ``sys_session_message`` does not forward replies."""
    uid = uuid.uuid4().hex[:8]
    job = _start_job(rig, worker_harness, uid=uid, first_turn=[{"text": f"FIRST-{uid}"}])
    _wait_first_read(rig, job)

    peer_model = f"mock-peer-{uid}"
    peer_bundle = rig.bundles / f"later-reply-peer-{uid}"
    peer_bundle.mkdir()
    peer = _agent_spec(
        rig,
        name=f"later-reply-peer-{uid}",
        harness="openai-agents",
        model=peer_model,
        prompt="Message the worker session when asked.",
    )
    (peer_bundle / "config.yaml").write_text(yaml.safe_dump(peer), encoding="utf-8")
    configure_mock_llm(
        rig.mock_url,
        [
            _tool_call(
                "openai-agents",
                "sys_session_message",
                {"session_id": job.worker_id, "message": f"peer-question-{uid}: quick question"},
                "peer-message",
            ),
            {"text": "Messaged the worker."},
        ],
        key=peer_model,
    )
    _script_worker(rig, worker_harness, f"peer-question-{uid}", [{"text": f"PEER-REPLY-{uid}"}])
    peer_id = create_runner_bound_session(
        rig.client, agent_name=upload_agent(rig.client, peer_bundle), runner_id=rig.runner_id
    )
    _send(rig, peer_id, "Message the worker.")
    _wait_for(
        lambda: f"PEER-REPLY-{uid}" in _text(rig, job.worker_id),
        description="the worker to answer the other session",
    )

    _script_worker(rig, worker_harness, f"user-follow-up-{uid}", [{"text": f"SECOND-{uid}"}])
    _send(rig, job.worker_id, f"user-follow-up-{uid}: one more thing")
    _assert_parent_reads(rig, job, f"SECOND-{uid}", absent=f"PEER-REPLY-{uid}")


@pytest.mark.parametrize("worker_harness", ["openai-agents", "claude-sdk"])
def test_reply_after_the_worker_helper_finishes_reaches_the_parent(
    rig: _Rig, worker_harness: str
) -> None:
    """The worker hands part of the job to its own helper and replies once it is back."""
    uid = uuid.uuid4().hex[:8]
    helper_model = f"mock-helper-{uid}"
    helper = _agent_spec(
        rig,
        name="helper",
        harness="openai-agents",
        model=helper_model,
        prompt="You are the helper. Answer in one short reply.",
    )
    configure_mock_llm(rig.mock_url, [{"text": f"HELPER-DONE-{uid}"}], key=helper_model)
    job = _start_job(
        rig,
        worker_harness,
        uid=uid,
        first_turn=[
            _tool_call(
                worker_harness,
                "sys_session_send",
                {"agent": "helper", "title": "part", "args": f"helper-task-{uid}: help"},
                "ask-helper",
            ),
            {"text": f"FIRST-{uid}: waiting on my helper"},
            _tool_call(worker_harness, "sys_read_inbox", {}, "worker-read"),
            {"text": f"SECOND-{uid}: my helper is done"},
        ],
        worker_extra={"async": True},
        worker_agents={"helper": helper},
    )
    try:
        _wait_for(
            lambda: f"HELPER-DONE-{uid}" in _inbox_reads(rig, job.worker_id),
            description="the worker to read its helper's result",
        )
    except AssertionError as exc:
        transcript = _text(rig, job.worker_id)[-1500:]
        raise AssertionError(f"{exc}; worker transcript tail: {transcript}") from None
    _assert_parent_reads(rig, job, f"SECOND-{uid}")


def test_claude_code_turn_started_by_itself_reaches_the_parent(rig: _Rig) -> None:
    """Claude Code finishes a background agent after its first reply and continues alone."""
    uid = uuid.uuid4().hex[:8]
    fork_prompt = f"background-investigation-{uid}-{uid}: look into it"
    # The background agent's reply waits on the mock gate until the parent has read
    # the worker's first reply. Its longer token routes the fork's own requests here.
    configure_mock_llm(
        rig.mock_url,
        [{"text": "INVESTIGATION FINDINGS: nothing unusual.", "block": True}],
        match=fork_prompt.split(":", 1)[0],
        required_tools=["Bash"],
    )
    job = _start_job(
        rig,
        "claude-native",
        uid=uid,
        first_turn=[
            _tool_call(
                "claude-native",
                "Agent",
                {
                    "subagent_type": "fork",
                    "description": "Background investigation",
                    "prompt": fork_prompt,
                },
                f"toolu_fork_{uid}",
            ),
            {"text": f"FIRST-{uid}: started a background investigation"},
            {"text": f"SECOND-{uid}: the background investigation is done"},
        ],
    )
    _wait_first_read(rig, job)
    release_mock_gate(rig.mock_url)
    _assert_parent_reads(rig, job, f"SECOND-{uid}")
