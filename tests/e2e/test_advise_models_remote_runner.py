"""E2E: a runner attached to a remote routing-capable server offers sys_advise_models.

The server is the only process that can answer "can this deployment route?"
(its ``/v1/info`` computes ``smart_routing_enabled`` from the routing backends
constructed in the server process). A runner attached to that server over a
host daemon holds no routing backends of its own, so the tool-registration
gate must not be answered from the runner's local caps — otherwise
``sys_advise_models`` is hidden from every session on that runner even though
the server would happily serve the call.

The journey is the real remote-attachment path, driven end to end:

1. the shared e2e server (mock mode configures a server ``llm:`` block, so
   the built-in judge makes ``smart_routing_enabled: true``),
2. a real ``omnigent host`` daemon registered against it,
3. a session launched on that host's runner,
4. one turn against the mock LLM — whose captured request body carries the
   exact tool surface the model was offered.

The assertion is on that captured tool surface: ``sys_list_models`` present
(the sub-agent tool block registered and the capture worked) and
``sys_advise_models`` present alongside it (the routing-gated tool was not
hidden). All against the mock LLM server — no real credentials needed::

    .venv/bin/python -m pytest tests/e2e/test_advise_models_remote_runner.py -v
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.conftest import (
    POLL_INTERVAL_S,
    configure_mock_llm,
    lookup_agent_id,
    poll_session_until_terminal,
    register_inline_agent,
    send_user_message_to_session,
)

#: Seconds to wait for the host daemon to register online.
_HOST_ONLINE_TIMEOUT_S = 60.0

#: Seconds to wait for the host-launched runner to come online.
_RUNNER_ONLINE_TIMEOUT_S = 45.0


def _spawn_host_daemon(
    *,
    tmp_path: Path,
    live_server: str,
) -> tuple[subprocess.Popen[bytes], str, Path]:
    """Spawn an isolated ``omnigent host`` daemon against *live_server*.

    The daemon gets a private ``HOME`` plus explicit ``OMNIGENT_CONFIG_HOME``
    / ``OMNIGENT_DATA_DIR`` under *tmp_path*, so an ambient config home (e.g.
    one whose provider block references env vars this machine doesn't have)
    can never leak into the runner it launches. The host identity is
    pre-seeded at ``$HOME/.omnigent/config.yaml`` — the path the daemon's
    identity loader actually reads — and ``OMNIGENT_CONFIG_HOME`` points at
    the same directory so config reads agree with it.

    :param tmp_path: Per-test temp dir used as the daemon's ``HOME``.
    :param live_server: Server URL the daemon registers with.
    :returns: ``(proc, host_id, daemon_log)``.
    """
    config_home = tmp_path / ".omnigent"
    config_home.mkdir(parents=True, exist_ok=True)
    host_id = uuid.uuid4().hex
    host_name = f"e2e-advise-{uuid.uuid4().hex[:12]}"
    (config_home / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": host_name}},
            default_flow_style=False,
            sort_keys=True,
        )
    )
    daemon_log = tmp_path / "host-daemon.log"
    # Pin the daemon (and the runner it launches) to this worktree's omnigent:
    # the assertion is about RUNNER-side tool registration, so an ambient
    # PYTHONPATH naming another omnigent checkout would silently test that
    # other build. apply_runner_env drops the variable again in compat mode,
    # where the pinned old install must resolve instead.
    repo_root = Path(__file__).resolve().parents[2]
    ambient_pythonpath = os.environ.get("PYTHONPATH", "")
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_DATA_DIR": str(tmp_path / "omnigent-data"),
        "PYTHONPATH": f"{repo_root}{os.pathsep}{ambient_pythonpath}",
        PROCESS_LOG_FILE_ENV_VAR: str(daemon_log),
    }
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            [runner_executable(), "-m", "omnigent.host._daemon_entry", "--server", live_server],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    return proc, host_id, daemon_log


def _wait_for_host_online(client: httpx.Client, host_id: str, timeout: float) -> None:
    """Poll ``GET /v1/hosts`` until *host_id* shows online.

    :param client: HTTP client pointed at the server.
    :param host_id: Host id to wait for.
    :param timeout: Max seconds to wait.
    :raises AssertionError: When the host never registers.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = client.get("/v1/hosts")
            if resp.status_code == 200:
                for host in resp.json().get("hosts", []):
                    if host["host_id"] == host_id and host["status"] == "online":
                        return
        except httpx.ConnectError:
            pass
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"Host {host_id!r} did not appear online within {timeout}s")


def _offered_tool_names(mock_llm_server_url: str, model: str) -> set[str]:
    """Return every tool name the mock LLM saw for requests to *model*.

    The mock server captures each request body verbatim; the ``tools`` array
    is exactly the tool surface the runner's ToolManager advertised for the
    turn. Handles both Responses-style (top-level ``name``) and
    chat-completions-style (``function.name``) tool entries.

    :param mock_llm_server_url: Base URL of the mock LLM server.
    :param model: The model key the agent's requests carry.
    :returns: The union of tool names across the captured requests.
    """
    resp = httpx.get(f"{mock_llm_server_url}/mock/requests", params={"key": model}, timeout=10)
    resp.raise_for_status()
    names: set[str] = set()
    for request in resp.json().get("requests", []):
        for tool in request.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            name = tool.get("name") or (tool.get("function") or {}).get("name")
            if isinstance(name, str):
                names.add(name)
    return names


def test_advise_models_offered_on_runner_attached_to_routing_server(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    mock_llm_server_url: str,
) -> None:
    """A session on a host-attached runner is offered ``sys_advise_models``.

    Preconditions asserted, not assumed: the server itself reports
    ``smart_routing_enabled: true`` on ``/v1/info`` (in mock mode the e2e
    server carries an ``llm:`` block, which builds the built-in judge). When
    the deployment cannot route the journey's premise is absent, so skip.

    **What breaks if wrong:** the ``sys_advise_models`` registration gate is
    evaluated against the runner process's local caps — which never carry a
    routing backend on a remote-server attachment — so the tool is hidden
    from every session on the runner while the same server advertises
    ``smart_routing_enabled: true`` and would have answered the call.
    """
    info_resp = http_client.get("/v1/info")
    info_resp.raise_for_status()
    info = info_resp.json()
    if not info.get("smart_routing_enabled"):
        pytest.skip(
            "this server deployment cannot route (smart_routing_enabled is "
            "false), so the advisor is correctly hidden and the journey's "
            "premise is absent"
        )

    # A unique model key claims a private request log slice on the mock
    # server, so a stray request from another test can't pollute the capture.
    model = f"mock-advise-remote-{uuid.uuid4().hex[:6]}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "OK"}] * 3,
        key=model,
    )
    # spawn: true grants the sub-agent tool block, where sys_list_models is
    # unconditional and sys_advise_models is routing-gated — the pair under test.
    agent_name = register_inline_agent(
        http_client,
        name=f"advise-remote-{uuid.uuid4().hex[:6]}",
        harness="openai-agents",
        model=model,
        profile="",
        prompt="You are a terse smoke-test assistant. Follow instructions exactly.",
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
        extra_config={"spawn": True},
    )
    agent_id = lookup_agent_id(http_client, agent_name)

    daemon_proc, host_id, daemon_log = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
    )
    try:
        _wait_for_host_online(http_client, host_id, timeout=_HOST_ONLINE_TIMEOUT_S)

        # The remote-attachment path: create the session, then launch a
        # runner for it on the host and bind it — the same flow the UI's
        # host picker drives.
        create_resp = http_client.post("/v1/sessions", json={"agent_id": agent_id})
        create_resp.raise_for_status()
        session_id = create_resp.json()["id"]

        workspace = tmp_path / "ws"
        workspace.mkdir()
        launch_resp = http_client.post(
            f"/v1/hosts/{host_id}/runners",
            json={"session_id": session_id, "workspace": str(workspace)},
            timeout=90.0,
        )
        assert launch_resp.status_code == 200, (
            f"Runner launch failed: {launch_resp.status_code} {launch_resp.text}"
        )
        runner_id = launch_resp.json()["runner_id"]

        deadline = time.monotonic() + _RUNNER_ONLINE_TIMEOUT_S
        while time.monotonic() < deadline:
            status_resp = http_client.get(f"/v1/runners/{runner_id}/status")
            if status_resp.status_code == 200 and status_resp.json().get("online") is True:
                break
            time.sleep(POLL_INTERVAL_S)
        else:
            daemon_tail = daemon_log.read_text(errors="replace")[-3000:]
            raise AssertionError(
                f"Runner {runner_id} never came online after launch.\n"
                f"host daemon log tail:\n{daemon_tail}"
            )
        http_client.patch(
            f"/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
        ).raise_for_status()

        # One turn is enough: the request the harness sends to the (mock)
        # model carries the full tool surface for the session.
        response_id = send_user_message_to_session(
            http_client,
            session_id=session_id,
            content="Reply with exactly OK and nothing else. Do not call tools.",
        )
        body = poll_session_until_terminal(
            http_client,
            session_id=session_id,
            response_id=response_id,
            timeout=120,
        )
        assert body["status"] == "completed", (
            f"Turn on the host-attached runner failed: {body.get('error')}"
        )

        offered = _offered_tool_names(mock_llm_server_url, model)
        # Control first: the unconditional sibling tool from the same
        # registration block proves the capture observed the real surface.
        assert any(name.endswith("sys_list_models") for name in offered), (
            f"sys_list_models missing from the offered tools — the capture "
            f"did not observe the sub-agent tool block at all. Offered: "
            f"{sorted(offered)}"
        )
        assert any(name.endswith("sys_advise_models") for name in offered), (
            f"The server reports smart_routing_enabled: true, but the "
            f"session on the host-attached runner was not offered "
            f"sys_advise_models. The registration gate is being answered "
            f"from the runner's local caps (which carry no routing backend "
            f"on a remote-server attachment) instead of the server's "
            f"routing capability. Offered tools: {sorted(offered)}"
        )
    finally:
        daemon_proc.send_signal(signal.SIGTERM)
        try:
            daemon_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            daemon_proc.kill()
            daemon_proc.wait(timeout=5)
