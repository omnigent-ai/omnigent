"""E2E: a mis-routed host-backed session must self-heal, not strand.

On a multi-replica deployment a session's runner tunnel lives on one replica,
and the ingress can route the session's later requests to another. The replica
without the tunnel answers every send with 400 ``wrong_replica`` ("session
runner is on another replica; retry"), and nothing recovers the session: the
user retries into the same error forever.

The stack here is the smallest real multi-replica deployment: two
``omnigent server`` processes sharing one SQLite database, plus a real
``omnigent host`` daemon dialed into replica B, so B holds the host and runner
tunnels. Driving the SPA served by replica A plays the ingress mis-route: same
session, same database, no tunnel on the answering replica.

The regression guard asserts the user-visible outcome: after a working turn on
replica B, a follow-up sent while routed to replica A must still produce a
reply (whichever layer heals the mis-route). While the bug is live the
follow-up strands on a wrong-replica error pill, a user retry fails the same
way, and the test fails at that point.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import pytest
from playwright.sync_api import Page, Response, expect

from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SPA_DIST = _REPO_ROOT / "omnigent" / "server" / "static" / "web-ui"

_COMPOSER_LABEL = "Message the agent"
_MODEL = "replica-misroute-probe"
_AGENT_NAME = "replica_misroute_probe"
_AGENT_YAML = f"""\
name: {_AGENT_NAME}
prompt: You are a terse assistant.

executor:
  model: {_MODEL}
  harness: openai-agents
"""

_TURN1_TEXT = "hello from the tunnel replica"
_TURN1_REPLY = "reply one: the host-backed session works"
_FOLLOWUP_TEXT = "follow-up sent after the ingress mis-route"
_FOLLOWUP_REPLY = "reply two: the mis-routed send was healed"

_HEALTH_TIMEOUT_S = 90.0
_HOST_ONLINE_TIMEOUT_S = 60.0
_RUNNER_ONLINE_TIMEOUT_S = 120.0
_POLL_S = 0.5


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _tail(path: Path) -> str:
    return path.read_text(errors="replace")[-3000:] if path.exists() else "<no log>"


@dataclass
class _ReplicaStack:
    """Two replicas over one DB; the host/runner tunnels live on ``url_b``."""

    url_a: str
    url_b: str
    host_id: str
    agent_id: str
    workspace: Path


@pytest.fixture
def replica_stack(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> Iterator[_ReplicaStack]:
    """Boot replica B (+agent), replica A on the same DB, and a host on B.

    :yields: The stack handle with both base URLs, the online host id, and
        the registered probe agent's id.
    """
    db_path = tmp_path / "shared.db"
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent_yaml = tmp_path / f"{_AGENT_NAME}.yaml"
    agent_yaml.write_text(_AGENT_YAML)

    # Absolute-only PYTHONPATH: the host daemon forks runners with cwd="/",
    # where any relative ambient entry (e.g. "sdks/python-client") resolves
    # to nothing and the runner dies on import.
    pythonpath = os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
        ]
        + [p for p in os.environ.get("PYTHONPATH", "").split(os.pathsep) if os.path.isabs(p)]
    )
    env = {
        **os.environ,
        "PYTHONPATH": pythonpath,
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
        "OMNIGENT_WEB_UI_DIST": str(_SPA_DIST),
    }

    procs: list[subprocess.Popen[bytes]] = []
    logs: dict[str, Path] = {}

    def _spawn_replica(label: str, port: int, *, register_agent: bool) -> None:
        log_path = tmp_path / f"server-{label}.log"
        logs[label] = log_path
        argv = [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifacts),
        ]
        if register_agent:
            argv += ["--agent", str(agent_yaml)]
        with open(log_path, "w") as log_handle:
            procs.append(
                subprocess.Popen(argv, env=env, stdout=log_handle, stderr=subprocess.STDOUT)
            )

    def _wait_health(label: str, url: str) -> None:
        proc = procs[-1]
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(
                    f"replica {label} exited early (code {proc.returncode}):\n{_tail(logs[label])}"
                )
            try:
                if httpx.get(f"{url}/health", timeout=2.0).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(_POLL_S)
        raise RuntimeError(f"replica {label} not healthy in time:\n{_tail(logs[label])}")

    def _teardown() -> None:
        for proc in reversed(procs):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in reversed(procs):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

    try:
        port_b, port_a = _free_port(), _free_port()
        url_b, url_a = f"http://127.0.0.1:{port_b}", f"http://127.0.0.1:{port_a}"

        # B boots first so it alone runs first-boot migrations + registration.
        _spawn_replica("b", port_b, register_agent=True)
        _wait_health("b", url_b)
        _spawn_replica("a", port_a, register_agent=False)
        _wait_health("a", url_a)

        host_config_home = tmp_path / "host-config"
        host_config_home.mkdir()
        host_env = {
            **env,
            "OMNIGENT_CONFIG_HOME": str(host_config_home),
            "OMNIGENT_DATA_DIR": str(tmp_path / "host-data"),
        }
        host_env.pop("CLAUDECODE", None)
        host_log = tmp_path / "host.log"
        logs["host"] = host_log
        with open(host_log, "w") as host_handle:
            procs.append(
                subprocess.Popen(
                    [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", url_b],
                    env=host_env,
                    stdout=subprocess.DEVNULL,
                    stderr=host_handle,
                )
            )

        host_proc = procs[-1]
        host_id: str | None = None
        deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
        while time.monotonic() < deadline:
            if host_proc.poll() is not None:
                raise RuntimeError(f"host daemon exited early:\n{_tail(host_log)}")
            resp = httpx.get(f"{url_b}/v1/hosts", timeout=5.0)
            if resp.status_code == 200:
                online = [h for h in resp.json().get("hosts", []) if h.get("status") == "online"]
                if online:
                    host_id = str(online[0]["host_id"])
                    break
            time.sleep(_POLL_S)
        if host_id is None:
            raise RuntimeError(f"no host came online on replica B:\n{_tail(host_log)}")

        agents = httpx.get(f"{url_b}/v1/agents", timeout=10.0)
        agents.raise_for_status()
        agent_id = next((a["id"] for a in agents.json()["data"] if a["name"] == _AGENT_NAME), None)
        assert agent_id is not None, f"agent {_AGENT_NAME!r} was not registered on replica B"

        configure_mock_llm(
            mock_llm_server_url, [{"text": _TURN1_REPLY}], key=f"{_MODEL}-turn1", match=_TURN1_TEXT
        )
        configure_mock_llm(
            mock_llm_server_url,
            [{"text": _FOLLOWUP_REPLY}],
            key=f"{_MODEL}-followup",
            match=_FOLLOWUP_TEXT,
        )
        set_fallback_mock_llm(mock_llm_server_url, _MODEL, "ok")

        yield _ReplicaStack(
            url_a=url_a, url_b=url_b, host_id=host_id, agent_id=agent_id, workspace=workspace
        )
    finally:
        _teardown()


@pytest.fixture
def misrouted_session(replica_stack: _ReplicaStack) -> str:
    """A host-backed session on replica B with its runner online.

    Created before the ``page`` fixture opens the browser so the recorded
    journey starts at the first navigation instead of a blank page.

    :returns: The session id.
    """
    create = httpx.post(
        f"{replica_stack.url_b}/v1/sessions",
        json={
            "agent_id": replica_stack.agent_id,
            "host_id": replica_stack.host_id,
            "workspace": str(replica_stack.workspace),
        },
        timeout=60.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["id"])
    _wait_for_online_runner(replica_stack.url_b, session_id)
    return session_id


def _wait_for_online_runner(url_b: str, session_id: str) -> None:
    """Block until the session's host-launched runner tunnel is up on B."""
    runner_id: str | None = None
    deadline = time.monotonic() + _RUNNER_ONLINE_TIMEOUT_S
    while time.monotonic() < deadline:
        if runner_id is None:
            snap = httpx.get(f"{url_b}/v1/sessions/{session_id}", timeout=5.0)
            if snap.status_code == 200:
                runner_id = snap.json().get("runner_id") or None
        if runner_id is not None:
            status = httpx.get(f"{url_b}/v1/runners/{runner_id}/status", timeout=5.0)
            if status.status_code == 200 and status.json().get("online") is True:
                return
        time.sleep(_POLL_S)
    raise AssertionError(
        f"session {session_id} runner (id={runner_id!r}) never came online on replica B"
    )


def _send(page: Page, text: str) -> None:
    page.get_by_label(_COMPOSER_LABEL).fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _demonstrate_stranding_and_fail(page: Page, wrong_replica_errors: list[str]) -> None:
    """Show the user's retry failing the same way, then fail the test."""
    pill = page.get_by_test_id("error-pill").first
    expect(pill).to_be_visible(timeout=15_000)
    pill.click()
    expect(page.get_by_test_id("error-message-content").first).to_contain_text(
        "another replica", timeout=10_000
    )
    page.wait_for_timeout(1_500)
    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_have_value(_FOLLOWUP_TEXT, timeout=15_000)
    failures_before_retry = len(wrong_replica_errors)
    page.get_by_role("button", name="Send", exact=True).click()
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline and len(wrong_replica_errors) <= failures_before_retry:
        page.wait_for_timeout(200)
    retry_failed_too = len(wrong_replica_errors) > failures_before_retry
    page.wait_for_timeout(2_000)
    pytest.fail(
        "the mis-routed session never self-healed: the follow-up send failed with "
        f"wrong_replica ({wrong_replica_errors[0]!r}), "
        + (
            "the user's retry failed the same way, "
            if retry_failed_too
            else "the user's retry produced no reply either, "
        )
        + "and no assistant reply ever arrived on the mis-routed replica"
    )


def test_misrouted_session_send_self_heals(
    replica_stack: _ReplicaStack, misrouted_session: str, page: Page
) -> None:
    """A follow-up sent via the replica without the tunnel still gets a reply."""
    stack = replica_stack
    session_id = misrouted_session

    wrong_replica_errors: list[str] = []

    def _capture_wrong_replica(response: Response) -> None:
        try:
            if (
                response.request.method == "POST"
                and urlparse(response.url).path == f"/v1/sessions/{session_id}/events"
                and response.status == 400
            ):
                error = response.json().get("error", {})
                if error.get("code") == "wrong_replica":
                    wrong_replica_errors.append(str(error.get("message", "")))
        except Exception:
            # A torn-down response body must not kill the listener.
            pass

    page.on("response", _capture_wrong_replica)

    # Turn 1 on replica B (the tunnel-holding replica): the session works.
    page.goto(f"{stack.url_b}/c/{session_id}")
    expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)
    _send(page, _TURN1_TEXT)
    expect(page.get_by_text(_TURN1_REPLY)).to_be_visible(timeout=90_000)
    page.wait_for_timeout(1_000)

    # The mis-route: the same session's requests now land on replica A.
    # Turn 1's reply proves the transcript loaded there from the shared DB.
    page.goto(f"{stack.url_a}/c/{session_id}")
    expect(page.get_by_text(_TURN1_REPLY)).to_be_visible(timeout=30_000)
    expect(page.get_by_label(_COMPOSER_LABEL)).to_be_visible(timeout=30_000)
    page.wait_for_timeout(500)
    _send(page, _FOLLOWUP_TEXT)

    reply = page.get_by_text(_FOLLOWUP_REPLY)
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if reply.count() > 0:
            break
        if wrong_replica_errors:
            _demonstrate_stranding_and_fail(page, wrong_replica_errors)
        page.wait_for_timeout(200)
    expect(reply).to_be_visible(timeout=10_000)
