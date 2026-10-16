"""E2E: ACP sessions display the harness's active model, not the agent spec's pin.

An agent whose spec pins an executor model (``hello_world`` pins ``gpt-4o-mini``)
is launched with the Grok Build ACP harness override. Before the ACP process
reports a model, the composer must show the harness identity; after the first
turn it must show, and keep across a reload, the model the process reported.

The rig is a dedicated server + runner with an isolated ``HOME`` /
``OMNIGENT_CONFIG_HOME`` whose ``config.yaml`` points the builtin ``grok`` row
at a hermetic fake Grok Build agent (an ACP-over-stdio script), so no developer
or CI configuration is read or written.
"""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _BUILD_OUTPUT, _REPO_ROOT, _TEST_AGENT_YAML, _find_free_port

# The builtin agent the rig registers; its spec pins ``executor.model``.
_PINNED_AGENT_NAME = "hello_world"
_SPEC_PINNED_MODEL = "gpt-4o-mini"

# What the fake Grok Build agent reports as its active model, and the harness
# identity the composer must show before any report.
_ACP_ACTIVE_MODEL = "grok-4.6"
_HARNESS_IDENTITY = "Grok Build"
_ACP_REPLY_TEXT = "Grok Build reply: hello from fake-grok"

# Boot budget for the spawned server + runner pair on a loaded CI box.
_RIG_TIMEOUT_S = 90.0
_RIG_POLL_INTERVAL_S = 0.5

# Proxy-blind client: CI's egress proxy must not intercept loopback requests.
_client = httpx.Client(trust_env=False)

# Grok Build stand-in speaking the Agent Client Protocol over stdio: reports its
# model as a ``model`` config option in the ``session/new`` result, then streams
# one reply and completes the turn with usage that carries no model key.
_FAKE_GROK_AGENT = r"""#!/usr/bin/env python3
import sys, json

def send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid, method = msg.get("id"), msg.get("method")
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"promptCapabilities": {"image": False}},
        }})
    elif method == "session/new":
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "sessionId": "fake-grok-session-1",
            "configOptions": [
                {"id": "model", "name": "Model", "currentValue": "grok-4.6"},
            ],
        }})
    elif method == "session/prompt":
        sid = msg["params"]["sessionId"]
        send({"jsonrpc": "2.0", "method": "session/update",
              "params": {"sessionId": sid, "update": {
                  "sessionUpdate": "agent_message_chunk",
                  "content": {"type": "text",
                              "text": "Grok Build reply: hello from fake-grok"}}}})
        send({"jsonrpc": "2.0", "id": mid, "result": {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 3, "outputTokens": 5, "totalTokens": 8},
        }})
"""


@dataclass
class _AcpRig:
    """A dedicated server + runner pair with an isolated home and config."""

    base_url: str
    runner_id: str


def _builtin_agent_id(base_url: str, name: str) -> str:
    """Resolve a builtin agent's id by name from ``GET /v1/agents``."""
    resp = _client.get(f"{base_url}/v1/agents?limit=100", timeout=10.0)
    resp.raise_for_status()
    agent = next((a for a in resp.json()["data"] if a["name"] == name), None)
    if agent is None:
        pytest.fail(f"Builtin agent {name!r} is not registered on the rig at {base_url}.")
    return str(agent["id"])


def _wait_until_online(
    base_url: str,
    runner_id: str,
    procs: list[subprocess.Popen[bytes]],
    work: Path,
) -> None:
    """Block until the rig's server is healthy and its runner reports online."""
    deadline = time.monotonic() + _RIG_TIMEOUT_S
    while time.monotonic() < deadline:
        if any(proc.poll() is not None for proc in procs):
            break
        try:
            if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                status = _client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                if status.status_code == 200 and status.json().get("online"):
                    return
        except httpx.HTTPError:
            pass
        time.sleep(_RIG_POLL_INTERVAL_S)
    raise RuntimeError(
        f"ACP e2e rig did not come online within {_RIG_TIMEOUT_S:.0f}s.\n"
        f"Server log:\n{(work / 'server.log').read_text()[-3000:]}\n"
        f"Runner log:\n{(work / 'runner.log').read_text()[-3000:]}"
    )


@pytest.fixture
def acp_rig(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[_AcpRig]:
    """Spawn an isolated server + runner whose ``grok`` harness row runs the fake agent."""
    if request.config.getoption("--ui-base-url"):
        pytest.skip("the ACP reported-model e2e requires an isolated spawned server")

    work = tmp_path_factory.mktemp("acp_reported_model")
    config_home = work / "config-home"
    home_dir = work / "home"
    artifacts = work / "artifacts"
    for path in (config_home, home_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)

    fake_grok = work / "fake-grok"
    fake_grok.write_text(_FAKE_GROK_AGENT)
    fake_grok.chmod(0o755)
    # The runner re-reads this config at dispatch, so the fake command is in
    # place for the first turn without touching any real config.yaml.
    (config_home / "config.yaml").write_text(
        yaml.safe_dump({"harness": {"grok": {"command": str(fake_grok)}}})
    )
    agent_yaml = work / f"{_PINNED_AGENT_NAME}.yaml"
    agent_yaml.write_text(_TEST_AGENT_YAML)

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)

    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)
    no_proxy = ",".join(filter(None, [os.environ.get("NO_PROXY", ""), "127.0.0.1,localhost"]))
    shared_env = {
        **os.environ,
        # Import omnigent from the worktree, not a stale installed copy.
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "HOME": str(home_dir),
        "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

    server_log = (work / "server.log").open("w")
    runner_log = (work / "runner.log").open("w")
    procs: list[subprocess.Popen[bytes]] = []
    try:
        procs.append(
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
                    f"sqlite:///{work}/test.db",
                    "--artifact-location",
                    str(artifacts),
                    "--agent",
                    str(agent_yaml),
                ],
                env=server_env,
                stdout=server_log,
                stderr=subprocess.STDOUT,
                cwd=str(_REPO_ROOT),
            )
        )
        procs.append(
            subprocess.Popen(
                [sys.executable, "-m", "omnigent.runner._entry"],
                env=runner_env,
                stdout=runner_log,
                stderr=subprocess.STDOUT,
                cwd=str(_REPO_ROOT),
            )
        )
        _wait_until_online(base_url, runner_id, procs, work)
        yield _AcpRig(base_url=base_url, runner_id=runner_id)
    finally:
        for proc in procs:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in procs:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        server_log.close()
        runner_log.close()


@pytest.fixture
def grok_override_session(acp_rig: _AcpRig) -> tuple[str, str]:
    """A session on the pinned-model agent, created with ``harness_override: "grok"``.

    The same JSON create the web new-chat harness picker issues, bound to the
    rig's runner so the first turn dispatches to the fake agent.

    :returns: ``(base_url, session_id)``.
    """
    agent_id = _builtin_agent_id(acp_rig.base_url, _PINNED_AGENT_NAME)
    create = _client.post(
        f"{acp_rig.base_url}/v1/sessions",
        json={"agent_id": agent_id, "harness_override": "grok"},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["id"])
    bind = _client.patch(
        f"{acp_rig.base_url}/v1/sessions/{session_id}",
        json={"runner_id": acp_rig.runner_id},
        timeout=10.0,
    )
    bind.raise_for_status()
    return (acp_rig.base_url, session_id)


def test_fresh_acp_session_shows_harness_identity_not_spec_model(
    page: Page,
    grok_override_session: tuple[str, str],
) -> None:
    """Before any model report, the composer shows the harness identity, not the spec pin."""
    base_url, session_id = grok_override_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)

    # The positive expectation doubles as the snapshot-hydration wait.
    config_value = page.get_by_test_id("composer-agent-config-value")
    expect(config_value).to_be_visible(timeout=30_000)
    expect(config_value).to_contain_text(_HARNESS_IDENTITY, timeout=30_000)
    expect(config_value).not_to_contain_text(_SPEC_PINNED_MODEL)


def test_acp_reported_model_persists_and_displays(
    page: Page,
    grok_override_session: tuple[str, str],
) -> None:
    """After a turn, the composer shows the reported model live and again after a reload."""
    base_url, session_id = grok_override_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Say hello")
    composer.press("Enter")

    # Once the fake agent's reply renders, its model report has reached the server.
    expect(page.get_by_text(_ACP_REPLY_TEXT)).to_be_visible(timeout=90_000)
    model_value = page.get_by_test_id("composer-agent-model-value")
    expect(model_value).to_contain_text(_ACP_ACTIVE_MODEL, timeout=30_000)
    expect(model_value).not_to_contain_text(_SPEC_PINNED_MODEL)

    page.reload()
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
    model_value = page.get_by_test_id("composer-agent-model-value")
    expect(model_value).to_contain_text(_ACP_ACTIVE_MODEL, timeout=30_000)
    expect(model_value).not_to_contain_text(_SPEC_PINNED_MODEL)
