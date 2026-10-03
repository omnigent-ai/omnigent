"""Verify a scheduled Codex task runs unattended with the real CLI."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import find_free_port
from tests.e2e.helpers import HEALTH_TIMEOUT_S, POLL_INTERVAL_S

_REPO_ROOT = Path(__file__).resolve().parents[2]

_CODEX_AGENT_NAME = "codex-native-ui"
_CODEX_MIN_VERSION = (0, 139, 0)
_CODEX_MOCK_MODEL = "mock-model"

_HOST_ONLINE_TIMEOUT_S = 90.0
_FIRE_RUN_TIMEOUT_S = 120.0
_UNATTENDED_COMPLETION_TIMEOUT_S = 240.0


def _codex_cli_supports_app_server(codex_path: str) -> bool:
    """Return whether the installed Codex CLI is new enough for the mock lane."""
    probe = subprocess.run([codex_path, "--version"], text=True, capture_output=True, check=False)
    if probe.returncode != 0:
        return False
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", f"{probe.stdout}\n{probe.stderr}")
    if not match:
        return False
    return tuple(int(part) for part in match.groups()) >= _CODEX_MIN_VERSION


@dataclass(frozen=True)
class _ScheduledCodexStack:
    """Spawned server + host stack for scheduled codex-native fires."""

    base_url: str
    mock_url: str
    host_id: str
    workspace: Path
    outside_dir: Path


def _wait_health(url: str, procs: list[subprocess.Popen], timeout_s: float) -> str | None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for proc in procs:
            if proc.poll() is not None:
                return f"process {proc.args[0]}... exited early rc={proc.returncode}"
        try:
            if httpx.get(url, timeout=2).status_code == 200:
                return None
        except httpx.HTTPError:
            pass
        time.sleep(POLL_INTERVAL_S)
    return f"no HTTP 200 from {url} within {timeout_s:.0f}s"


@pytest.fixture(scope="module")
def scheduled_codex_stack(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_ScheduledCodexStack]:
    """Route a server and host daemon through the mock Responses API."""
    tmp = tmp_path_factory.mktemp("codex_sched_stack")
    home = tmp / "home"
    config_home = tmp / "config-home"
    codex_home = tmp / "codex-home"
    workspace = home / "workspace"
    outside_dir = home / "outside"
    artifacts = tmp / "artifacts"
    for path in (home, config_home, codex_home, workspace, outside_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)

    mock_port = find_free_port()
    server_port = find_free_port()
    mock_url = f"http://127.0.0.1:{mock_port}"
    base_url = f"http://127.0.0.1:{server_port}"

    (config_home / "config.yaml").write_text(
        f"""\
providers:
  codex-e2e-mock:
    kind: key
    default: openai
    openai:
      base_url: "{mock_url}/v1"
      api_key: "sk-e2e-mock"
      wire_api: responses
      models:
        default: {_CODEX_MOCK_MODEL}
""",
        encoding="utf-8",
    )

    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "CODEX_HOME": str(codex_home),
        "HOME": str(home),
        "OPENAI_BASE_URL": f"{mock_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }
    env.pop("OMNIGENT_RUNNER_TUNNEL_TOKEN", None)

    logs = {
        name: open(tmp / f"{name}.log", "w")  # noqa: SIM115
        for name in ("mock", "server", "host")
    }
    procs: list[subprocess.Popen] = []
    try:
        procs.append(
            subprocess.Popen(
                [
                    sys.executable,
                    str(_REPO_ROOT / "tests/server/integration/mock_llm_server.py"),
                    str(mock_port),
                ],
                env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
                stdout=logs["mock"],
                stderr=subprocess.STDOUT,
            )
        )
        error = _wait_health(f"{mock_url}/stats", procs, 30.0)
        if error is not None:
            raise RuntimeError(f"mock LLM did not start: {error}")

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
                    str(server_port),
                    "--database-uri",
                    f"sqlite:///{tmp}/test.db",
                    "--artifact-location",
                    str(artifacts),
                ],
                env=env,
                stdout=logs["server"],
                stderr=subprocess.STDOUT,
            )
        )
        error = _wait_health(f"{base_url}/health", procs, HEALTH_TIMEOUT_S * 2)
        if error is not None:
            server_log = (tmp / "server.log").read_text()[-3000:]
            raise RuntimeError(f"server did not start: {error}\n{server_log}")

        procs.append(
            subprocess.Popen(
                [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", base_url],
                env=env,
                stdout=logs["host"],
                stderr=subprocess.STDOUT,
            )
        )
        host_id: str | None = None
        deadline = time.monotonic() + _HOST_ONLINE_TIMEOUT_S
        while time.monotonic() < deadline:
            try:
                hosts = httpx.get(f"{base_url}/v1/hosts", timeout=5).json().get("hosts", [])
                online = [h for h in hosts if h.get("status") == "online"]
                if online:
                    host_id = str(online[0]["host_id"])
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        if host_id is None:
            host_log = (tmp / "host.log").read_text()[-3000:]
            raise RuntimeError(f"host daemon never came online:\n{host_log}")

        yield _ScheduledCodexStack(
            base_url=base_url,
            mock_url=mock_url,
            host_id=host_id,
            workspace=workspace,
            outside_dir=outside_dir,
        )
    finally:
        for proc in reversed(procs):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in reversed(procs):
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        for handle in logs.values():
            handle.close()


def _codex_agent_id(base_url: str) -> str:
    agents = httpx.get(f"{base_url}/v1/agents", timeout=30).json()["data"]
    for agent in agents:
        if agent["name"] == _CODEX_AGENT_NAME:
            return str(agent["id"])
    raise AssertionError(f"{_CODEX_AGENT_NAME!r} not auto-registered on the server")


@pytest.mark.timeout(600)
def test_codex_scheduled_fire_with_bypass_completes_unattended(
    scheduled_codex_stack: _ScheduledCodexStack,
) -> None:
    """A bypass-opted scheduled task writes outside its workspace without approval."""
    codex_path = shutil.which("codex")
    if codex_path is None:
        pytest.skip("codex CLI is required for the scheduled codex fire e2e")
    if not _codex_cli_supports_app_server(codex_path):
        pytest.skip("codex CLI >= 0.139.0 is required for the scheduled codex fire e2e")
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for runner-owned codex terminals")

    stack = scheduled_codex_stack
    marker = f"sched-bypass-{uuid.uuid4().hex[:8]}"
    artifact_path = stack.outside_dir / f"artifact-{marker}.txt"

    apply_patch_cmd = (
        "apply_patch <<'EOF'\n"
        "*** Begin Patch\n"
        f"*** Add File: {artifact_path}\n"
        "+scheduled artifact\n"
        "*** End Patch\n"
        "EOF\n"
    )
    httpx.post(
        f"{stack.mock_url}/mock/configure",
        json={
            "key": marker,
            "match": marker,
            "responses": [
                {
                    "tool_calls": [
                        {
                            "call_id": f"call-{marker}",
                            "name": "exec_command",
                            "arguments": json.dumps({"cmd": apply_patch_cmd}),
                        }
                    ],
                }
            ],
        },
        timeout=10,
    ).raise_for_status()
    # Keep unscripted approval attempts from writing the artifact.
    httpx.post(
        f"{stack.mock_url}/mock/set_fallback",
        json={"key": _CODEX_MOCK_MODEL, "response": {"text": "Done."}},
        timeout=10,
    ).raise_for_status()

    created = httpx.post(
        f"{stack.base_url}/v1/scheduled-tasks",
        json={
            "name": "unattended codex artifact task",
            "prompt": f"{marker} write the scheduled artifact file",
            "rrule": "FREQ=HOURLY",
            "agent_id": _codex_agent_id(stack.base_url),
            "host_id": stack.host_id,
            "workspace": str(stack.workspace),
            "permission_mode": "bypassPermissions",
        },
        timeout=60,
    )
    assert created.status_code == 200, (
        "creating a codex-native scheduled task with bypassPermissions failed "
        f"(no unattended opt-out exists): HTTP {created.status_code}: {created.text}"
    )
    task_id = created.json()["id"]

    fired = httpx.post(f"{stack.base_url}/v1/scheduled-tasks/{task_id}/run", timeout=60)
    assert fired.status_code == 202, fired.text

    session_id: str | None = None
    deadline = time.monotonic() + _FIRE_RUN_TIMEOUT_S
    while time.monotonic() < deadline:
        runs = httpx.get(f"{stack.base_url}/v1/scheduled-tasks/{task_id}/runs", timeout=10).json()
        rows = runs.get("runs") or []
        if rows and rows[0].get("conversation_id"):
            session_id = str(rows[0]["conversation_id"])
            break
        time.sleep(2)
    assert session_id is not None, "fire never recorded a run with a session"

    deadline = time.monotonic() + _UNATTENDED_COMPLETION_TIMEOUT_S
    while time.monotonic() < deadline:
        snapshot = httpx.get(f"{stack.base_url}/v1/sessions/{session_id}", timeout=10)
        pending = (
            snapshot.json().get("pending_elicitations") or []
            if snapshot.status_code == 200
            else []
        )
        assert not pending, (
            "unattended codex fire parked on a pending approval elicitation "
            f"(the reported stall): {json.dumps(pending)[:500]}"
        )
        if artifact_path.exists():
            break
        time.sleep(3)

    assert artifact_path.exists(), (
        "unattended fire never wrote the artifact: the bypassPermissions "
        "opt-in did not reach Codex's bypass launch stance"
    )
    assert artifact_path.read_text().strip() == "scheduled artifact"
