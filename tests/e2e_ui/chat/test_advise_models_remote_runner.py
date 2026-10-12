"""UI regression: ``sys_advise_models`` hidden on a runner attached to a routing server.

Journey (what the user does and sees in the SPA):

1. Run an omnigent server with smart routing configured (a server-level
   ``llm:`` block builds the built-in judge), so ``GET /v1/info`` reports
   ``smart_routing_enabled: true``.
2. Attach a separate runner process to that server — either the sibling
   runner every local deployment spawns, or an ``omnigent host --server``
   daemon that launches the session's runner on its machine.
3. Open a session on that runner whose agent orchestrates a sub-agent and ask
   which model to use for a task. The agent reaches for its routing advisor.
4. The tool surface offered to the model lacks ``sys_advise_models`` even
   though its sibling ``sys_list_models`` is present, so the turn fails and
   the chat shows an error pill naming the missing tool instead of a
   recommendation.

Both tests FAIL on un-fixed code and must PASS once a runner attached to a
routing-enabled server advertises the advisor.

Run (spawns its own routing-enabled server, sibling runner and host daemon;
build the SPA first)::

    pytest tests/e2e_ui/chat/test_advise_models_remote_runner.py -v --ui-skip-build
"""

from __future__ import annotations

import io
import json
import os
import secrets
import signal
import subprocess
import sys
import tarfile
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from omnigent.runner.identity import token_bound_runner_id
from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable
from tests.e2e_ui.conftest import (
    _BUILD_OUTPUT,
    _HEALTH_POLL_INTERVAL_S,
    _REPO_ROOT,
    _find_free_port,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)

_T = TypeVar("_T")

# Queue keys on the mock LLM: the orchestrator brain and the server's judge.
_BRAIN_MODEL = "mock-advisor-orch-brain"
_JUDGE_MODEL = "mock-routing-judge"
_PARENT_NAME = "advisor_orch"
_TASK = "refactor the auth module"

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_STACK_READY_TIMEOUT_S = 120.0
_HOST_REGISTER_TIMEOUT_S = 90.0
_TURN_TIMEOUT_MS = 180_000

_LEAKED_RUNNER_ENV_PREFIXES = ("OMNIGENT_RUNNER_", "OMNIGENT_HOST_")
_LEAKED_RUNNER_ENV = frozenset({"RUNNER_SERVER_URL", "OMNIGENT_REMOTE_AUTH_TOKEN"})


def _agent_yaml(mock_llm_server_url: str) -> str:
    """Orchestrator spec: an openai-agents brain with one declared worker.

    The declared sub-agent registers ``sys_session_send`` / ``sys_list_models``
    on the brain, the same grant that gates ``sys_advise_models``. An explicit
    ``auth`` block pins the brain to the mock so an ambient provider config
    cannot shadow it.
    """
    return f"""\
name: {_PARENT_NAME}
prompt: |
  You orchestrate one `worker` sub-agent. When the user asks which model to
  use for a task, call `sys_advise_models` for that task first and then
  summarize the recommendation.

executor:
  model: {_BRAIN_MODEL}
  harness: openai-agents
  auth:
    type: api_key
    api_key: mock-key
    base_url: {mock_llm_server_url}/v1

tools:
  worker:
    type: agent
    description: General-purpose worker sub-agent.
    executor:
      model: gpt-4o-mini
      harness: openai-agents
    prompt: |
      You are the worker sub-agent.

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""


def _clean_env() -> dict[str, str]:
    """Copy of ``os.environ`` without an enclosing runner's identity/tunnel vars."""
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(_LEAKED_RUNNER_ENV_PREFIXES) and key not in _LEAKED_RUNNER_ENV
    }


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _tail(path: Path) -> str:
    return path.read_text(errors="replace")[-3000:] if path.exists() else ""


def _wait_for(
    predicate: Callable[[], _T | None],
    *,
    timeout_s: float,
    what: str,
    logs: dict[str, Path],
) -> _T:
    """Poll *predicate* until it returns a value; fail with the process logs.

    :param predicate: Returns the awaited value, or ``None`` to keep waiting.
    :param timeout_s: Max seconds to wait.
    :param what: Description for the failure message.
    :param logs: Log files whose tails are appended to the failure.
    :returns: The predicate's first non-``None`` value.
    :raises RuntimeError: When *timeout_s* elapses first.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value is not None:
            return value
        time.sleep(_HEALTH_POLL_INTERVAL_S)
    tails = "\n".join(f"{name}:\n{_tail(path)}" for name, path in logs.items())
    raise RuntimeError(f"timed out after {timeout_s:.0f}s waiting for {what}.\n{tails}")


def _runner_online(base_url: str, runner_id: str) -> bool:
    try:
        status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
    except httpx.HTTPError:
        return False
    return status.status_code == 200 and status.json().get("online") is True


@dataclass(frozen=True)
class _RoutingStack:
    """A routing-enabled server plus the sibling runner attached to it.

    :param base_url: Spawned server base URL.
    :param runner_id: Token-bound id of the sibling runner process.
    :param agent_id: Id of the orchestrator registered as a built-in agent.
    :param mock_url: Mock LLM server every process talks to.
    :param root: Temp dir holding the stack's logs.
    """

    base_url: str
    runner_id: str
    agent_id: str
    mock_url: str
    root: Path


@pytest.fixture(scope="module")
def routing_stack(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_RoutingStack]:
    """Spawn ``omnigent server --config <llm: block>`` and a sibling runner.

    Modeled on the suite's ``live_server``, with the one difference the report
    needs: the server carries an ``llm:`` block, so its built-in judge exists
    and ``/v1/info`` advertises smart routing. The runner is a separate OS
    process tunneled into the server, exactly as a deployment's runner is.

    :param built_spa: Ensures the SPA bundle is on disk before the server boots.
    :param mock_llm_server_url: Session-scoped mock LLM server URL.
    :param tmp_path_factory: Pytest temp path factory for logs, DB, config.
    :yields: The stack handle.
    """
    root = tmp_path_factory.mktemp("advise_models_routing_stack")
    server_cfg = root / "server.yaml"
    server_cfg.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "model": _JUDGE_MODEL,
                    "connection": {
                        "base_url": f"{mock_llm_server_url}/v1",
                        "api_key": "mock-key",
                    },
                }
            }
        )
    )
    agent_yaml_path = root / f"{_PARENT_NAME}.yaml"
    agent_yaml_path.write_text(_agent_yaml(mock_llm_server_url))
    set_fallback_mock_llm(
        mock_llm_server_url,
        _JUDGE_MODEL,
        json.dumps({"model": "gpt-4o-mini", "harness": "worker", "rationale": "mock judge"}),
    )

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    server_env = {
        **_clean_env(),
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
        "OMNIGENT_BUILTIN_AGENT_DIRS": str(agent_yaml_path),
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
        "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
    }
    apply_server_env(server_env, _REPO_ROOT)
    server_log_path = root / "server.log"
    server_log = open(server_log_path, "w")  # noqa: SIM115 — lives for the subprocess
    server = subprocess.Popen(
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
            f"sqlite:///{root / 'sessions.db'}",
            "--artifact-location",
            str(root / "artifacts"),
            "--config",
            str(server_cfg),
        ],
        env=server_env,
        cwd=compat_server_cwd(),
        stdout=server_log,
        stderr=subprocess.STDOUT,
    )

    runner_env = {
        **_clean_env(),
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }
    runner_log_path = root / "runner.log"
    runner_log = open(runner_log_path, "w")  # noqa: SIM115 — lives for the subprocess
    runner = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=runner_env,
        stdout=runner_log,
        stderr=subprocess.STDOUT,
    )
    logs = {"server.log": server_log_path, "runner.log": runner_log_path}

    def _ready() -> bool | None:
        if server.poll() is not None or runner.poll() is not None:
            raise RuntimeError(
                f"routing stack exited early (server={server.poll()}, runner={runner.poll()}).\n"
                f"server.log:\n{_tail(server_log_path)}\nrunner.log:\n{_tail(runner_log_path)}"
            )
        try:
            healthy = httpx.get(f"{base_url}/health", timeout=2).status_code == 200
        except httpx.HTTPError:
            return None
        return True if healthy and _runner_online(base_url, runner_id) else None

    try:
        _wait_for(
            _ready,
            timeout_s=_STACK_READY_TIMEOUT_S,
            what="the routing server and its sibling runner",
            logs=logs,
        )
        agents = httpx.get(f"{base_url}/v1/agents", params={"limit": 200}, timeout=10.0)
        agents.raise_for_status()
        agent_id = next(a["id"] for a in agents.json()["data"] if a["name"] == _PARENT_NAME)
        yield _RoutingStack(
            base_url=base_url,
            runner_id=runner_id,
            agent_id=agent_id,
            mock_url=mock_llm_server_url,
            root=root,
        )
    finally:
        _stop(runner)
        _stop(server)
        runner_log.close()
        server_log.close()


@pytest.fixture(scope="module")
def attached_host(routing_stack: _RoutingStack) -> Iterator[str]:
    """Register an ``omnigent host --server`` daemon against the routing server.

    The daemon gets an isolated config home and data dir so it never reads or
    writes ``~/.omnigent``; the sessions the server launches on it run in a
    runner process the daemon spawns.

    :param routing_stack: The routing-enabled server to attach to.
    :yields: The registered host's ``host_id``.
    """
    root = routing_stack.root / "host"
    config_home = root / "config-home"
    config_home.mkdir(parents=True)
    host_env = {
        **_clean_env(),
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_DATA_DIR": str(root / "data"),
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_LOG_TO_STDERR": "1",
        "OPENAI_BASE_URL": f"{routing_stack.mock_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }
    host_log_path = root / "host.log"
    host_log = open(host_log_path, "w")  # noqa: SIM115 — lives for the subprocess
    host = subprocess.Popen(
        [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", routing_stack.base_url],
        env=host_env,
        stdout=subprocess.DEVNULL,
        stderr=host_log,
    )

    def _online_host() -> str | None:
        if host.poll() is not None:
            raise RuntimeError(
                f"host daemon exited early with code {host.returncode}.\n"
                f"host.log:\n{_tail(host_log_path)}"
            )
        try:
            hosts = httpx.get(f"{routing_stack.base_url}/v1/hosts", timeout=3).json()
        except (httpx.HTTPError, ValueError):
            return None
        online = [h for h in hosts.get("hosts", []) if h.get("status") == "online"]
        return str(online[0]["host_id"]) if online else None

    try:
        yield _wait_for(
            _online_host,
            timeout_s=_HOST_REGISTER_TIMEOUT_S,
            what="the host daemon to register",
            logs={"host.log": host_log_path},
        )
    finally:
        _stop(host)
        host_log.close()


@dataclass(frozen=True)
class _AdvisorSession:
    """Handle for an orchestrator session on an attached runner.

    :param base_url: Spawned server base URL.
    :param session_id: The session id.
    :param routing_token: Per-run token that selects the brain's mock queue.
    """

    base_url: str
    session_id: str
    routing_token: str


def _script_advisor_turn(mock_url: str) -> str:
    """Queue the brain's advisor call followed by a marker reply.

    ``required_tools`` keeps the scripted call for the orchestrator's own turn
    (which advertises ``sys_list_models``) rather than a tool-less title
    request, without demanding the advisor be offered — the point of the
    journey is what happens when it is not.

    :param mock_url: Mock LLM server base URL.
    :returns: The routing token the user's message must carry.
    """
    routing_token = f"advise-models-{uuid.uuid4().hex[:10]}"
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"call-advise-{routing_token}",
                        "name": "sys_advise_models",
                        "arguments": json.dumps(
                            {
                                "tasks": [
                                    {
                                        "title": "auth-refactor",
                                        "agents": [{"agent": "worker", "models": None}],
                                        "task": _TASK,
                                    }
                                ]
                            }
                        ),
                    }
                ]
            },
            {"text": f"Advisor consulted. Marker: {routing_token}"},
        ],
        key=_BRAIN_MODEL,
        match=routing_token,
        required_tools=["sys_list_models"],
    )
    return routing_token


@pytest.fixture
def sibling_runner_session(routing_stack: _RoutingStack) -> Iterator[_AdvisorSession]:
    """Orchestrator session bound to the sibling runner.

    :param routing_stack: The routing-enabled server + sibling runner.
    :yields: An :class:`_AdvisorSession` handle.
    """
    routing_token = _script_advisor_turn(routing_stack.mock_url)
    yaml_bytes = _agent_yaml(routing_stack.mock_url).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # Non-config.yaml arcname routes the bundle through the omnigent
        # compat adapter, whose loader parses the inline `type: agent` tool.
        info = tarfile.TarInfo(name=f"{_PARENT_NAME}.yaml")
        info.size = len(yaml_bytes)
        tar.addfile(info, io.BytesIO(yaml_bytes))
    create_resp = httpx.post(
        f"{routing_stack.base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    httpx.patch(
        f"{routing_stack.base_url}/v1/sessions/{session_id}",
        json={"runner_id": routing_stack.runner_id},
        timeout=10.0,
    ).raise_for_status()

    try:
        yield _AdvisorSession(
            base_url=routing_stack.base_url,
            session_id=session_id,
            routing_token=routing_token,
        )
    finally:
        try:
            httpx.delete(f"{routing_stack.base_url}/v1/sessions/{session_id}", timeout=10.0)
        finally:
            reset_mock_llm(routing_stack.mock_url)


@pytest.fixture
def host_runner_session(
    routing_stack: _RoutingStack,
    attached_host: str,
    tmp_path: Path,
) -> Iterator[_AdvisorSession]:
    """Orchestrator session the server launches on the attached host.

    :param routing_stack: The routing-enabled server.
    :param attached_host: The registered host's id.
    :param tmp_path: Workspace directory for the launch.
    :yields: An :class:`_AdvisorSession` handle.
    """
    routing_token = _script_advisor_turn(routing_stack.mock_url)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    create_resp = httpx.post(
        f"{routing_stack.base_url}/v1/sessions",
        json={
            "agent_id": routing_stack.agent_id,
            "host_id": attached_host,
            "workspace": str(workspace),
        },
        timeout=120.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["id"]

    def _launched_runner_online() -> bool | None:
        snapshot = httpx.get(f"{routing_stack.base_url}/v1/sessions/{session_id}", timeout=10.0)
        snapshot.raise_for_status()
        runner_id = snapshot.json().get("runner_id")
        return True if runner_id and _runner_online(routing_stack.base_url, runner_id) else None

    try:
        _wait_for(
            _launched_runner_online,
            timeout_s=_STACK_READY_TIMEOUT_S,
            what="the host-launched runner to come online",
            logs={"host.log": routing_stack.root / "host" / "host.log"},
        )
        yield _AdvisorSession(
            base_url=routing_stack.base_url,
            session_id=session_id,
            routing_token=routing_token,
        )
    finally:
        try:
            httpx.delete(f"{routing_stack.base_url}/v1/sessions/{session_id}", timeout=30.0)
        finally:
            reset_mock_llm(routing_stack.mock_url)


def _request_text(request: dict[str, object]) -> str:
    """Flatten a captured Responses-API request's input into one string."""
    parts: list[str] = []

    def _walk(node: object) -> None:
        if isinstance(node, str):
            parts.append(node)
        elif isinstance(node, dict):
            for value in node.values():
                _walk(value)
        elif isinstance(node, list):
            for value in node:
                _walk(value)

    _walk(request.get("input"))
    return " ".join(parts)


def _offered_tools(mock_url: str, routing_token: str) -> set[str]:
    """Tool names the brain was offered on the turn carrying *routing_token*.

    :param mock_url: Mock LLM server base URL.
    :param routing_token: The marker sent in the user's message.
    :returns: Names from the request's ``tools`` list.
    """
    resp = httpx.get(f"{mock_url}/mock/requests", params={"key": _BRAIN_MODEL}, timeout=10.0)
    resp.raise_for_status()
    turns = [r for r in resp.json()["requests"] if routing_token in _request_text(r)]
    assert turns, f"the mock LLM saw no {_BRAIN_MODEL} request carrying {routing_token!r}"
    names: set[str] = set()
    for tool in turns[0].get("tools") or []:
        schema = tool.get("function", tool)
        if isinstance(schema, dict) and isinstance(schema.get("name"), str):
            names.add(schema["name"])
    return names


def _open_chat(page: Page, chat: _AdvisorSession) -> None:
    """Open the session and make sure the chat composer is the main view.

    A host-launched session opens terminal-first; its Chat/Terminal pill
    switches back to the composer.
    """
    page.goto(f"{chat.base_url}/c/{chat.session_id}")
    composer = page.get_by_role("textbox", name="Message the agent")
    toggle = page.get_by_test_id("view-mode-toggle")
    expect(composer.or_(toggle).first).to_be_visible(timeout=30_000)
    if not composer.is_visible():
        toggle.get_by_role("button", name="Chat view").click()
    expect(composer).to_be_visible(timeout=30_000)


def _assert_advisor_offered(page: Page, stack: _RoutingStack, chat: _AdvisorSession) -> None:
    """Drive the journey and assert the runner offered the advisor.

    :param page: pytest-playwright page fixture.
    :param stack: The routing-enabled server the session's runner is attached to.
    :param chat: The orchestrator session handle.
    """
    info = httpx.get(f"{stack.base_url}/v1/info", timeout=10.0).json()
    assert info["smart_routing_enabled"] is True, info["smart_routing_sources"]

    _open_chat(page, chat)
    page.get_by_role("textbox", name="Message the agent").fill(
        f"Which model should I use for this task: {_TASK}? Routing marker: {chat.routing_token}"
    )
    page.get_by_role("button", name="Send", exact=True).click()

    reply = page.locator(_ASSISTANT, has_text=f"Advisor consulted. Marker: {chat.routing_token}")
    error_pill = page.get_by_test_id("error-pill")
    expect(reply.or_(error_pill).first).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    expect(page.get_by_test_id("working-indicator")).to_be_hidden(timeout=30_000)

    failure = ""
    if error_pill.count():
        # Expand the pill so the failing tool name is on screen (and in the video).
        error_pill.first.get_by_role("button").first.click()
        expect(error_pill.first.get_by_test_id("error-message-content")).to_be_visible()
        failure = error_pill.first.inner_text()

    offered = _offered_tools(stack.mock_url, chat.routing_token)
    assert "sys_list_models" in offered, sorted(offered)
    assert "sys_advise_models" in offered, (
        "Bug reproduced: the runner attached to a routing-enabled server "
        f"(smart_routing_sources={info['smart_routing_sources']}) offered "
        f"{sorted(offered)} without sys_advise_models; the chat shows: {failure!r}"
    )
    expect(reply).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    expect(error_pill).to_have_count(0)


@pytest.mark.timeout(600)
def test_sibling_runner_attached_to_routing_server_offers_advise_models(
    page: Page,
    routing_stack: _RoutingStack,
    sibling_runner_session: _AdvisorSession,
) -> None:
    """The sibling runner a local deployment spawns must advertise the advisor.

    On un-fixed code the runner offered the model ``sys_list_models`` but not
    ``sys_advise_models``, so the turn dies with a "tool not found" error pill.

    :param page: pytest-playwright page fixture.
    :param routing_stack: The routing-enabled server + sibling runner.
    :param sibling_runner_session: The orchestrator session on that runner.
    """
    _assert_advisor_offered(page, routing_stack, sibling_runner_session)


@pytest.mark.timeout(600)
def test_host_attached_runner_offers_advise_models(
    page: Page,
    routing_stack: _RoutingStack,
    host_runner_session: _AdvisorSession,
) -> None:
    """A runner launched by an ``omnigent host --server`` daemon must advertise the advisor.

    Same failure shape as the sibling runner: the host machine's runner holds
    no routing backend of its own, so the advisor is hidden even though the
    server it attached to reports smart routing on.

    :param page: pytest-playwright page fixture.
    :param routing_stack: The routing-enabled server the host attached to.
    :param host_runner_session: The orchestrator session launched on the host.
    """
    _assert_advisor_offered(page, routing_stack, host_runner_session)
