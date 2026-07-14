"""E2E test for ``omnigent chat`` — local mode with mock LLM.

Verifies that ``omnigent chat ./agent-dir/`` starts a server, opens the
REPL, and the agent responds. Since the REPL is interactive, we
test by directly calling the local mode components rather than
launching the full CLI.

Runs entirely against the mock LLM server — no real API key needed::

    pytest tests/e2e/test_chat_e2e.py -v
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from pathlib import Path

import httpx
import pytest
import yaml as _yaml
from omnigent_client import OmnigentClient

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests.e2e.conftest import (
    configure_mock_llm,
    find_free_port,
    poll_session_until_terminal,
    reset_mock_llm,
    send_user_message_to_session,
)
from tests.e2e.helpers import final_assistant_text


def _lookup_builtin_agent_id(client: httpx.Client, agent_name: str) -> str:
    """
    Return the durable agent id for a built-in agent registered by name.

    Uses ``GET /v1/agents`` (the built-in agent discovery list) which
    includes agents registered via ``--agent`` at server startup, unlike
    ``GET /v1/sessions?agent_name=...`` which only finds agents that
    already have an associated session.

    :param client: HTTP client pointed at the live server.
    :param agent_name: Display name of the registered agent.
    :returns: The matching agent id, e.g. ``"ag_..."``.
    :raises AssertionError: If no agent with that name is found.
    """
    resp = client.get("/v1/agents", params={"limit": 100})
    resp.raise_for_status()
    for item in resp.json().get("data", []):
        if item.get("name") == agent_name:
            return str(item["id"])
    raise AssertionError(
        f"agent {agent_name!r} not found in GET /v1/agents. "
        f"Available: {[d.get('name') for d in resp.json().get('data', [])]}"
    )


def _create_runner_bound_session_for_builtin(
    client: httpx.Client,
    *,
    agent_name: str,
    runner_id: str,
) -> str:
    """
    Create a session bound to a built-in agent and to *runner_id*.

    Looks up the agent id via ``GET /v1/agents`` (not via existing
    sessions) so this works immediately after ``_start_local_server``
    before any session has been created.

    :param client: HTTP client pointed at the live server.
    :param agent_name: Display name of the registered built-in agent.
    :param runner_id: Registered runner id from ``server.runner_id``.
    :returns: The session/conversation id, e.g. ``"conv_abc"``.
    """
    agent_id = _lookup_builtin_agent_id(client, agent_name)
    resp = client.post(
        "/v1/sessions",
        json={"agent_id": agent_id},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    resp.raise_for_status()
    session_id = str(resp.json()["id"])
    resp = client.patch(
        f"/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
    )
    resp.raise_for_status()
    return session_id


def _make_inline_agent_yaml(
    tmp_path: Path,
    *,
    agent_name: str,
    model: str,
    mock_llm_server_url: str | None,
) -> Path:
    """
    Write a minimal openai-agents YAML wired to the mock LLM server.

    :param tmp_path: Directory to write the YAML into.
    :param agent_name: Agent ``name`` field.
    :param model: Mock model name (used as the mock queue key).
    :param mock_llm_server_url: Mock server base URL.
    :returns: Path to the written YAML file.
    """
    yaml_path = tmp_path / f"{agent_name}.yaml"
    yaml_path.write_text(
        _yaml.safe_dump(
            {
                "name": agent_name,
                "prompt": "You are a terse test assistant.",
                "executor": {
                    "harness": "openai-agents",
                    "model": model,
                    "profile": "",
                    "auth": {
                        "type": "api_key",
                        "api_key": "mock-key",
                        "base_url": f"{mock_llm_server_url}/v1",
                    },
                },
            }
        )
    )
    return yaml_path


def test_chat_local_starts_server_and_agent_responds(
    mock_llm_server_url: str | None,
    tmp_path: Path,
) -> None:
    """
    ``omnigent chat ./agent-dir/`` starts a local server with the agent
    and the agent can respond to messages.

    Tests the server startup and agent registration path used by
    ``omnigent chat`` in local mode. Since the REPL itself is interactive,
    we verify the underlying server works by sending a direct HTTP
    request through the sessions API.

    **What breaks if this fails:**
    - _start_local_server broken → server doesn't boot.
    - Agent bundle not registered → agent lookup in GET /v1/agents fails.
    - Agent config invalid → session turn fails.
    """
    from omnigent.chat import (
        _start_local_server,
        _stop_local_server,
        _wait_for_server,
    )

    marker = f"CHAT_LOCAL_OK_{uuid.uuid4().hex[:6]}"
    model = f"mock-chat-local-{uuid.uuid4().hex[:6]}"
    agent_name = f"chat-local-probe-{uuid.uuid4().hex[:6]}"

    # Inline YAML agent — openai-agents harness wired to mock model so no
    # real LLM is needed. OPENAI_BASE_URL / OPENAI_API_KEY are injected
    # into os.environ before _start_local_server so the child subprocess
    # inherits them ({**os.environ, ...} in _start_local_server's child_env).
    yaml_path = _make_inline_agent_yaml(
        tmp_path, agent_name=agent_name, model=model, mock_llm_server_url=mock_llm_server_url
    )

    # Set env vars before _start_local_server so the spawned server
    # subprocess inherits them through child_env = {**os.environ, ...}.
    os.environ["OPENAI_API_KEY"] = "mock-key"
    os.environ["OPENAI_BASE_URL"] = f"{mock_llm_server_url}/v1"

    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(mock_llm_server_url, [{"text": marker}], key=model)

    port = find_free_port()
    server = _start_local_server(yaml_path, port, ephemeral=True)
    base_url = f"http://127.0.0.1:{port}"

    try:
        _wait_for_server(port, server)

        client = httpx.Client(base_url=base_url, timeout=30.0)
        session_id = _create_runner_bound_session_for_builtin(
            client,
            agent_name=agent_name,
            runner_id=server.runner_id or "",
        )
        response_id = send_user_message_to_session(
            client,
            session_id=session_id,
            content="Say hello briefly.",
        )
        body = poll_session_until_terminal(
            client,
            session_id=session_id,
            response_id=response_id,
            timeout=120,
        )

        assert body["status"] == "completed", (
            f"Status: {body['status']!r}. Output: {body.get('output', [])}"
        )
        text = final_assistant_text(body)
        assert marker in text, f"Expected marker {marker!r} in agent output. Got: {text!r}"

    finally:
        _stop_local_server(server)


def test_chat_local_accepts_omnigent_yaml_file(
    mock_llm_server_url: str | None,
    tmp_path: Path,
) -> None:
    """
    ``omnigent chat examples/coding_supervisor.yaml`` (or any
    standalone omnigent YAML) now starts the local server and
    registers the agent under its spec-declared name.

    The YAML path exercises the new ``materialize_bundle`` code
    path in :func:`_preregister_agent`: a file source wraps into
    a bundle directory, gets tarred, and the stored tarball
    round-trips through :func:`omnigent.spec.load` to a
    validated :class:`AgentSpec`.

    **What breaks if this fails:**
    - ``_preregister_agent`` regresses to directory-only.
    - ``materialize_bundle``'s file branch produces the wrong
      dir shape and ``_find_omnigent_yaml_in_dir`` misses the
      YAML.
    - Agent-plane's spec dispatch stops routing omnigent YAMLs
      through ``load_omnigent_yaml``.

    :param mock_llm_server_url: Mock LLM server base URL.
    :param tmp_path: Per-test temp dir for the YAML fixture.
    """
    from omnigent.chat import (
        _start_local_server,
        _stop_local_server,
        _wait_for_server,
    )

    marker = f"CHAT_YAML_OK_{uuid.uuid4().hex[:6]}"
    model = f"mock-chat-yaml-{uuid.uuid4().hex[:6]}"
    agent_name = "yaml-e2e-probe"

    # Inline fixture: minimal omnigent YAML with the openai-agents harness
    # wired to the mock server. Self-contained so an edit to the real
    # ``examples/hello_world.yaml`` can't flake this test.
    yaml_path = tmp_path / "yaml-e2e-probe.yaml"
    yaml_path.write_text(
        _yaml.safe_dump(
            {
                "name": agent_name,
                "prompt": "You are a friendly assistant. Say hello briefly.",
                "executor": {
                    "model": model,
                    "harness": "openai-agents",
                    "profile": "",
                    "auth": {
                        "type": "api_key",
                        "api_key": "mock-key",
                        "base_url": f"{mock_llm_server_url}/v1",
                    },
                },
            }
        )
    )

    # Set env vars before _start_local_server so the spawned server
    # subprocess inherits them through child_env = {**os.environ, ...}.
    os.environ["OPENAI_API_KEY"] = "mock-key"
    os.environ["OPENAI_BASE_URL"] = f"{mock_llm_server_url}/v1"

    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(mock_llm_server_url, [{"text": marker}], key=model)

    port = find_free_port()
    server = _start_local_server(yaml_path, port, ephemeral=True)
    base_url = f"http://127.0.0.1:{port}"

    try:
        _wait_for_server(port, server)

        client = httpx.Client(base_url=base_url, timeout=30.0)

        # Agent is registered under the YAML's ``name`` field —
        # proves the materialize_bundle → tarball → load chain
        # preserves the spec name end-to-end.
        session_id = _create_runner_bound_session_for_builtin(
            client,
            agent_name=agent_name,
            runner_id=server.runner_id or "",
        )
        response_id = send_user_message_to_session(
            client,
            session_id=session_id,
            content="Say hello briefly.",
        )

        # A full turn proves the spec the server rehydrates from
        # the stored tarball also produces a runnable agent — not
        # just a registered-but-broken one. This is the single
        # strongest regression guard for the bundling refactor.
        body = poll_session_until_terminal(
            client,
            session_id=session_id,
            response_id=response_id,
            timeout=120,
        )

        assert body["status"] == "completed", (
            f"Status: {body['status']!r}. Output: {body.get('output', [])}"
        )
        text = final_assistant_text(body)
        assert marker in text, f"Expected marker {marker!r} in agent output. Got: {text!r}"
    finally:
        _stop_local_server(server)


def test_sdk_creates_registered_agent_session(
    mock_llm_server_url: str | None,
    tmp_path: Path,
) -> None:
    """
    The SDK can create a session from a server-registered agent.

    Starts a real server with an inline agent, resolves its durable id,
    and exercises the SDK's JSON session-creation contract.

    **What breaks if this fails:**
    - The SDK's registered-agent request no longer matches the server route.
    - The returned session cannot be bound to a runner through the SDK.
    """
    from omnigent.chat import (
        _start_local_server,
        _stop_local_server,
        _wait_for_server,
    )

    agent_name = f"pick-agent-probe-{uuid.uuid4().hex[:6]}"
    model = f"mock-pick-{uuid.uuid4().hex[:6]}"

    yaml_path = _make_inline_agent_yaml(
        tmp_path, agent_name=agent_name, model=model, mock_llm_server_url=mock_llm_server_url
    )

    # Set env vars before _start_local_server so the spawned server
    # subprocess inherits them through child_env = {**os.environ, ...}.
    os.environ["OPENAI_API_KEY"] = "mock-key"
    os.environ["OPENAI_BASE_URL"] = f"{mock_llm_server_url}/v1"

    port = find_free_port()
    server = _start_local_server(yaml_path, port, ephemeral=True)
    base_url = f"http://127.0.0.1:{port}"

    try:
        _wait_for_server(port, server)

        http_client = httpx.Client(base_url=base_url, timeout=30.0)
        agent_id = _lookup_builtin_agent_id(http_client, agent_name)

        async def _create_and_bind() -> tuple[str, str | None]:
            async with OmnigentClient(base_url=base_url) as client:
                session = await client.sessions.create_registered(agent_id)
                bound = await client.sessions.bind_runner(
                    session.id,
                    runner_id=server.runner_id or "",
                )
                return bound.id, bound.runner_id

        session_id, runner_id = asyncio.run(_create_and_bind())
        assert session_id.startswith("conv_")
        assert runner_id == server.runner_id
    finally:
        _stop_local_server(server)


def test_remote_prompt_creates_fresh_host_bound_session(
    mock_llm_server_url: str,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Remote headless chat discovers an agent and completes in a fresh session."""
    from omnigent.chat import (
        _start_local_server,
        _stop_local_server,
        _wait_for_server,
        run_prompt,
    )
    from tests.e2e.test_host_e2e import _spawn_host_daemon, _wait_for_host_online

    marker = f"REMOTE_FRESH_OK_{uuid.uuid4().hex[:6]}"
    model = f"mock-remote-fresh-{uuid.uuid4().hex[:6]}"
    agent_name = f"remote-fresh-probe-{uuid.uuid4().hex[:6]}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    host_home = tmp_path / "host-home"
    host_home.mkdir()

    yaml_path = _make_inline_agent_yaml(
        tmp_path,
        agent_name=agent_name,
        model=model,
        mock_llm_server_url=mock_llm_server_url,
    )
    os.environ["OPENAI_API_KEY"] = "mock-key"
    os.environ["OPENAI_BASE_URL"] = f"{mock_llm_server_url}/v1"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(mock_llm_server_url, [{"text": marker}], key=model)

    port = find_free_port()
    server = _start_local_server(yaml_path, port, ephemeral=True)
    base_url = f"http://127.0.0.1:{port}"
    daemon = None
    try:
        _wait_for_server(port, server)
        client = httpx.Client(
            base_url=base_url,
            timeout=60.0,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )
        agent_id = _lookup_builtin_agent_id(client, agent_name)
        daemon = _spawn_host_daemon(
            tmp_path=host_home,
            live_server=base_url,
            mock_llm_server_url=mock_llm_server_url,
        )
        _wait_for_host_online(client, daemon.host_id)

        seed = client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_id,
                "host_id": daemon.host_id,
                "workspace": str(workspace),
            },
        )
        seed.raise_for_status()
        seed_id = str(seed.json()["id"])
        client.post(
            f"/v1/sessions/{seed_id}/events",
            json={"type": "stop_session", "data": {}},
        ).raise_for_status()

        run_prompt(base_url, None, prompt="Reply with the configured marker.")

        assert marker in capsys.readouterr().out
        sessions = client.get(
            "/v1/sessions",
            params={"agent_id": agent_id, "limit": 10, "order": "desc"},
        )
        sessions.raise_for_status()
        rows = sessions.json()["data"]
        assert len(rows) == 2
        fresh = next(row for row in rows if row["id"] != seed_id)
        assert fresh["host_id"] == daemon.host_id
        assert fresh["workspace"] == str(workspace)
        assert fresh["runner_id"]
    finally:
        if daemon is not None:
            daemon.proc.terminate()
            try:
                daemon.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                daemon.proc.kill()
                daemon.proc.wait()
        _stop_local_server(server)
