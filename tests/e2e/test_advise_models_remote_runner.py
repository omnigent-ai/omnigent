"""E2E: ``sys_advise_models`` must be offered on a runner attached to a routing server.

Journey: run an omnigent server with
smart routing configured (the ``live_server`` fixture's server-level ``llm:``
block gives it the built-in OSS judge, so ``GET /v1/info`` reports
``smart_routing_enabled: true``) -> attach a runner process that does not share
the server's process (an ``omnigent host`` daemon, and the tunneled sibling
runner every local deployment spawns) -> start a session on that runner and run
one turn -> inspect the tool surface offered to the model.

Expected: ``sys_advise_models`` is advertised alongside ``sys_list_models``,
because the attached server can answer the call. The guarded regression: the
runner-process gate evaluates ``routing_available(get_caps())`` against its own
empty caps (the routing backends live only in the server process), so the
advisor is hidden from every session on such a runner unless the server's
session-init envelope carries its routing answer.

Usage::

    pytest tests/e2e/test_advise_models_remote_runner.py -v
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    get_mock_requests,
    lookup_agent_id,
    poll_session_until_terminal,
    register_inline_agent,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import POLL_INTERVAL_S

_REPO_ROOT = Path(__file__).resolve().parents[2]

_HOST_ONLINE_TIMEOUT_S = 60.0
_TURN_TIMEOUT_S = 180.0


def _assert_server_routes(client: httpx.Client) -> None:
    """Journey precondition: the server itself must report routing on."""
    resp = client.get("/v1/info")
    resp.raise_for_status()
    info = resp.json()
    assert info.get("smart_routing_enabled") is True, (
        "precondition failed: the test server does not report "
        f"smart_routing_enabled=true (got {info.get('smart_routing_enabled')!r}, "
        f"sources={info.get('smart_routing_sources')!r}); the live_server "
        "fixture's server-level llm: block should configure the OSS judge"
    )


def _register_spawn_agent(
    client: httpx.Client, mock_llm_server_url: str, *, model: str, label: str
) -> str:
    """Register an openai-agents agent whose ``spawn: true`` grant registers the
    sub-agent tool block (``sys_list_models`` + the gated ``sys_advise_models``)."""
    return register_inline_agent(
        client,
        name=f"advise-{label}-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a helpful assistant.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={"spawn": True},
    )


def _advertised_tool_names(reqs: list[dict[str, Any]]) -> set[str]:
    """Collect tool names offered to the model, normalizing MCP-style prefixes."""
    names: set[str] = set()
    for req in reqs:
        for tool in req.get("tools", []) or []:
            raw = tool.get("name") or (tool.get("function") or {}).get("name")
            if raw:
                names.add(str(raw).split("__")[-1])
    return names


def _run_turn_and_collect_tools(
    client: httpx.Client,
    mock_llm_server_url: str,
    *,
    session_id: str,
    model: str,
) -> set[str]:
    """Send one scripted turn and return the tool names its LLM request carried."""
    # A host-bound session's runner spawns asynchronously after create; the
    # events endpoint 503s until it tunnels in, so retry like the web UI does.
    deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
    while True:
        try:
            response_id = send_user_message_to_session(
                client,
                session_id=session_id,
                content="Which model should I use for this task?",
            )
            break
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 503 or time.monotonic() > deadline:
                raise
            time.sleep(POLL_INTERVAL_S)
    body = poll_session_until_terminal(
        client, session_id=session_id, response_id=response_id, timeout=_TURN_TIMEOUT_S
    )
    assert body["status"] == "completed", (
        f"turn did not complete: status={body['status']!r} error={body.get('error')!r}"
    )
    reqs = get_mock_requests(mock_llm_server_url, key=model)
    assert reqs, f"mock LLM captured no requests for model {model!r}"
    return _advertised_tool_names(reqs)


def _assert_advisor_offered(tool_names: set[str], *, topology: str) -> None:
    assert "sys_list_models" in tool_names, (
        f"control failed on the {topology}: sys_list_models missing, so the "
        f"sub-agent tool block never registered at all. Advertised: {sorted(tool_names)}"
    )
    assert "sys_advise_models" in tool_names, (
        f"sys_advise_models is hidden from a session on the {topology} even "
        "though the attached server reports smart_routing_enabled=true "
        f"(the runner-side routing_available(get_caps()) gate). "
        f"Advertised: {sorted(tool_names)}"
    )


@pytest.fixture
def attached_host(live_server: str, http_client: httpx.Client, tmp_path: Path) -> Iterator[str]:
    """Attach a real ``omnigent host`` daemon to the live server; yield its host id."""
    home_dir = tmp_path / "host-home"
    home_dir.mkdir()
    log_path = tmp_path / "host-daemon.log"
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["OMNIGENT_CONFIG_HOME"] = str(home_dir / "config")
    env["OMNIGENT_DATA_DIR"] = str(home_dir / "data")
    # Absolute paths only: the host-spawned runner's cwd is the session
    # workspace, where relative PYTHONPATH entries stop resolving.
    env["PYTHONPATH"] = os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
        ]
        + [e for e in env.get("PYTHONPATH", "").split(os.pathsep) if os.path.isabs(e)]
    )
    env.pop("CLAUDECODE", None)
    with open(log_path, "w") as log_fh:
        proc = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
        host_id: str | None = None
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise AssertionError(
                    f"host daemon exited with {proc.returncode}:\n"
                    f"{log_path.read_text(errors='replace')[-3000:]}"
                )
            resp = http_client.get("/v1/hosts")
            if resp.status_code == 200:
                online = [h for h in resp.json().get("hosts", []) if h["status"] == "online"]
                if online:
                    host_id = str(online[0]["host_id"])
                    break
            time.sleep(POLL_INTERVAL_S)
        if host_id is None:
            raise AssertionError(
                f"no host came online within {_HOST_ONLINE_TIMEOUT_S}s:\n"
                f"{log_path.read_text(errors='replace')[-3000:]}"
            )
        yield host_id
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def test_advise_models_offered_on_host_attached_runner(
    live_server: str,
    http_client: httpx.Client,
    mock_llm_server_url: str,
    attached_host: str,
    tmp_path: Path,
) -> None:
    """The reported journey: routing server -> ``omnigent host --server`` ->
    session on that host's runner -> the model must be offered ``sys_advise_models``."""
    _assert_server_routes(http_client)
    model = f"mock-advise-host-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)
    agent_name = _register_spawn_agent(http_client, mock_llm_server_url, model=model, label="host")
    configure_mock_llm(mock_llm_server_url, [{"text": "Acknowledged."}], key=model)

    workspace = tmp_path / "ws"
    workspace.mkdir()
    resp = http_client.post(
        "/v1/sessions",
        json={
            "agent_id": lookup_agent_id(http_client, agent_name),
            "host_id": attached_host,
            "workspace": str(workspace),
        },
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    resp.raise_for_status()
    session_id = str(resp.json()["id"])

    tool_names = _run_turn_and_collect_tools(
        http_client, mock_llm_server_url, session_id=session_id, model=model
    )
    _assert_advisor_offered(tool_names, topology="host-attached runner")


def test_advise_models_offered_on_tunneled_sibling_runner(
    live_server: str,
    http_client: httpx.Client,
    mock_llm_server_url: str,
    live_runner_id: str,
) -> None:
    """The wider scope from the issue thread: the default local topology's
    tunneled sibling runner must also be offered ``sys_advise_models``."""
    _assert_server_routes(http_client)
    model = f"mock-advise-sibling-{uuid.uuid4().hex[:6]}"
    reset_mock_llm(mock_llm_server_url)
    agent_name = _register_spawn_agent(
        http_client, mock_llm_server_url, model=model, label="sibling"
    )
    configure_mock_llm(mock_llm_server_url, [{"text": "Acknowledged."}], key=model)

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    tool_names = _run_turn_and_collect_tools(
        http_client, mock_llm_server_url, session_id=session_id, model=model
    )
    _assert_advisor_offered(tool_names, topology="tunneled sibling runner")
