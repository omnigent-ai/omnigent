"""End-to-end test for the isaac-native-ui built-in agent.

Verifies the host-spawned Web UI flow for ``isaac-native-ui``: list the
built-in agents -> find ``isaac-native-ui`` -> connect a host daemon ->
create a session bound to that agent and host -> assert the runner
auto-creates the Isaac terminal-mirror resource that the Web UI attaches
to.

Unlike the claude-native / codex-native e2e tests, Isaac is a pure
**terminal mirror**: there is no event forwarder, MCP bridge, or message
injection, so no assistant transcript items are mirrored to the server.
The durable, server-observable success signal is therefore the
**terminal resource** itself, not a transcript marker. This also lets the
test run in CI: the real ``isaac`` CLI is a Databricks-internal ``dbexec``
wrapper that can't run on a CI runner, so the test points
``OMNIGENT_ISAAC_COMMAND`` at a small fake ``isaac`` stub (prints a banner,
then blocks on stdin like an interactive TUI). The stub exercises the real
dispatch -> auto-create -> tmux-mirror path; it does not exercise Isaac's
own intelligence.

Run (opt-in via env var)::

    OMNIGENT_E2E_ISAAC_NATIVE=1 \
    .venv/bin/python -m pytest tests/e2e/test_host_isaac_native_e2e.py -v
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from omnigent.entities.session_resources import terminal_resource_id
from tests.e2e.helpers import POLL_INTERVAL_S

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_ISAAC_NATIVE") != "1",
    reason=(
        "isaac-native e2e is opt-in; set OMNIGENT_E2E_ISAAC_NATIVE=1 to run. "
        "It needs no real `isaac` binary — it stubs the CLI via "
        "OMNIGENT_ISAAC_COMMAND — so it is safe to run anywhere."
    ),
)

# The built-in agent the server auto-registers for the Web UI's "Isaac"
# option (see server.app._ensure_default_agents).
_ISAAC_NATIVE_AGENT_NAME = "isaac-native-ui"


def _write_fake_isaac(bin_dir: Path) -> Path:
    """
    Write a fake ``isaac`` CLI used in place of the real dbexec wrapper.

    The stub prints a recognizable banner and then blocks reading stdin,
    modeling an interactive TUI that stays alive in the pane. Pointing
    ``OMNIGENT_ISAAC_COMMAND`` at the absolute stub path makes the runner
    launch it instead of the real ``isaac`` (which needs dbexec OAuth and
    can't run in CI).

    :param bin_dir: Directory to write the stub into; created by caller.
    :returns: Absolute path to the executable stub.
    """
    stub = bin_dir / "fake-isaac"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'FAKE_ISAAC_TUI_READY'\n"
        "# Model an interactive TUI: stay alive holding the pane open.\n"
        "cat >/dev/null\n"
    )
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return stub


def _spawn_host_daemon(
    *,
    tmp_path: Path,
    live_server: str,
    isaac_command: str,
) -> subprocess.Popen[bytes]:
    """
    Spawn an ``omnigent host`` daemon with the fake ``isaac`` configured.

    :param tmp_path: Per-test temp dir for the daemon log.
    :param live_server: Test server URL the daemon registers with.
    :param isaac_command: Absolute path to the fake ``isaac`` stub, wired
        via ``OMNIGENT_ISAAC_COMMAND`` so the runner launches it.
    :returns: The spawned daemon subprocess handle.
    """
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["OMNIGENT_ISAAC_COMMAND"] = isaac_command
    daemon_log = tmp_path / "host-daemon.log"
    with open(daemon_log, "w") as log_fh:
        return subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.host._daemon_entry",
                "--server",
                live_server,
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )


def _online_host_id(client: httpx.Client, timeout: float = 30.0) -> str:
    """
    Poll ``GET /v1/hosts`` until at least one host is online.

    :param client: HTTP client pointed at the test server.
    :param timeout: Max seconds to wait.
    :returns: The online host's ``host_id``.
    :raises AssertionError: If no host comes online within *timeout*.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get("/v1/hosts")
        if resp.status_code == 200:
            online = [h for h in resp.json().get("hosts", []) if h["status"] == "online"]
            if online:
                return str(online[0]["host_id"])
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"No host came online within {timeout}s")


def _isaac_native_agent_id(client: httpx.Client) -> str:
    """
    Return the durable id of the auto-registered ``isaac-native-ui``.

    :param client: HTTP client pointed at the test server.
    :returns: The ``"ag_..."`` id for ``isaac-native-ui``.
    :raises AssertionError: If the server did not auto-register it.
    """
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    for agent in resp.json()["data"]:
        if agent["name"] == _ISAAC_NATIVE_AGENT_NAME:
            return str(agent["id"])
    raise AssertionError(
        f"{_ISAAC_NATIVE_AGENT_NAME!r} not registered on the server "
        "(expected from _ensure_default_agents at startup)"
    )


def _poll_for_terminal_resource(
    client: httpx.Client,
    *,
    session_id: str,
    resource_id: str,
    timeout: float,
) -> dict[str, object]:
    """
    Poll ``GET /v1/sessions/{id}/resources`` until *resource_id* appears.

    :param client: HTTP client pointed at the test server.
    :param session_id: Session/conversation id.
    :param resource_id: Expected terminal resource id, e.g.
        ``"terminal_isaac_main"``.
    :param timeout: Max seconds to wait.
    :returns: The matching terminal resource object.
    :raises AssertionError: If the resource never appears within *timeout*.
    """
    deadline = time.monotonic() + timeout
    last_seen: list[object] = []
    while time.monotonic() < deadline:
        resp = client.get(f"/v1/sessions/{session_id}/resources")
        if resp.status_code == 200:
            data = resp.json().get("data", [])
            last_seen = [r.get("id") for r in data]
            for resource in data:
                if resource.get("id") == resource_id and resource.get("type") == "terminal":
                    return resource
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(
        f"Terminal resource {resource_id!r} never appeared for session "
        f"{session_id} within {timeout}s; saw {last_seen!r}. The "
        "host-spawned isaac-native auto-create did not register the Isaac "
        "terminal, so the web UI would have no terminal mirror to attach to."
    )


def test_isaac_native_builtin_session_creates_terminal_mirror(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    Binding an isaac-native-ui session auto-creates the Isaac terminal.

    Happy path for the Isaac integration: the server auto-registers the
    built-in agent, a host daemon connects, and creating a session bound
    to that agent + host drives the runner's isaac-native auto-create,
    which launches the (fake) ``isaac`` TUI in a tmux pane and registers
    it as the ``terminal_isaac_main`` mirror resource. That resource is
    exactly what the Web UI attaches to, so its presence is the
    end-to-end proof the mirror is live.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_isaac = _write_fake_isaac(bin_dir)

    workspace = tmp_path / "ws"
    workspace.mkdir()

    daemon = _spawn_host_daemon(
        tmp_path=tmp_path,
        live_server=live_server,
        isaac_command=str(fake_isaac),
    )
    try:
        host_id = _online_host_id(http_client, timeout=30.0)
        agent_id = _isaac_native_agent_id(http_client)

        create = http_client.post(
            "/v1/sessions",
            json={
                "agent_id": agent_id,
                "host_id": host_id,
                "workspace": str(workspace),
            },
            timeout=60.0,
        )
        create.raise_for_status()
        session_id = create.json()["id"]

        resource = _poll_for_terminal_resource(
            http_client,
            session_id=session_id,
            resource_id=terminal_resource_id("isaac", "main"),
            timeout=30.0,
        )
        assert resource["type"] == "terminal"
    finally:
        daemon.send_signal(signal.SIGTERM)
        daemon.wait(timeout=5)
