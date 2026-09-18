"""Live journey: the runner idle watchdog reaps an idle runner.

The runner's inactivity watchdog shuts the process down after
``runner.idle_timeout_s`` of no agent work. This drives the full journey with
a compressed window: a real server + real runner boot, the runner goes
online, sits idle with no client attached, and its own watchdog reaps it --
after which the server reports the runner offline (the dead session a user
would find). The regression guard on the *default* idle window living long
enough to survive overnight is tests/runner/test_runner_idle_default_overnight.py.
"""

from __future__ import annotations

import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
# The spawned server/runner import the SDK clients that live outside the top
# package, so their subprocess PYTHONPATH must include the client/ui sdks.
_SPAWN_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
).rstrip(os.pathsep)

_HELLO_WORLD_AGENT = """name: hello_world
prompt: You are a friendly assistant. Say hello and answer questions.

executor:
  model: gpt-4o-mini
  harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _ambient_env() -> dict[str, str]:
    """The caller's env minus ambient OMNIGENT_* / RUNNER_* wiring.

    When this test itself runs inside an omnigent session, the inherited
    runner wiring (process-log path, runner identity, server URL) would
    redirect or crash the spawned server/runner; only the explicitly-set
    variables below may configure them.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OMNIGENT_", "RUNNER_"))
    }


@pytest.mark.timeout(240)
def test_idle_watchdog_reaps_idle_runner(tmp_path: Path) -> None:
    from omnigent.runner.identity import token_bound_runner_id

    # Long enough that a cold-started runner (imports + tunnel connect) goes
    # online before its own watchdog can fire; short enough to observe a reap
    # without an hour-long wait.
    idle_timeout_s = 30

    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        f"runner:\n  idle_timeout_s: {idle_timeout_s}\n", encoding="utf-8"
    )
    agent_yaml = tmp_path / "hello_world.yaml"
    agent_yaml.write_text(_HELLO_WORLD_AGENT, encoding="utf-8")

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    server_log = tmp_path / "server.log"
    runner_log = tmp_path / "runner.log"
    # The runner logs to a process log file, not stdout; point it at a path we
    # can read so the idle-shutdown reason is observable.
    runner_process_log = tmp_path / "runner_process.log"
    server_handle = open(server_log, "w", encoding="utf-8")  # noqa: SIM115
    runner_handle = open(runner_log, "w", encoding="utf-8")  # noqa: SIM115

    server_proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from omnigent.cli import main; main()",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{tmp_path / 'idle.db'}",
            "--artifact-location",
            str(tmp_path / "artifacts"),
            "--agent",
            str(agent_yaml),
        ],
        env={
            **_ambient_env(),
            "PYTHONPATH": _SPAWN_PYTHONPATH,
            "PYTHONUNBUFFERED": "1",
            "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
            "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
            "OPENAI_API_KEY": "mock-key",
            "ANTHROPIC_API_KEY": "",
        },
        stdout=server_handle,
        stderr=subprocess.STDOUT,
    )

    # The server must be up before the runner starts: the runner's idle clock
    # runs from its own boot, so server cold-start time must not eat into the
    # window the runner needs to come online.
    health_deadline = time.monotonic() + 120
    while time.monotonic() < health_deadline:
        if server_proc.poll() is not None:
            server_handle.close()
            pytest.fail(
                f"server exited (code={server_proc.returncode}) before healthy:\n"
                f"{server_log.read_text(encoding='utf-8')[-3000:]}"
            )
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    else:
        server_proc.terminate()
        server_handle.close()
        pytest.fail(
            f"server never became healthy:\n{server_log.read_text(encoding='utf-8')[-3000:]}"
        )

    runner_proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env={
            **_ambient_env(),
            "PYTHONPATH": _SPAWN_PYTHONPATH,
            "PYTHONUNBUFFERED": "1",
            "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_RUNNER_ID": runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": base_url,
            "OPENAI_API_KEY": "mock-key",
            "OMNIGENT_PROCESS_LOG_FILE": str(runner_process_log),
            "OMNIGENT_LOG_LEVEL": "INFO",
        },
        stdout=runner_handle,
        stderr=subprocess.STDOUT,
    )

    try:
        online_deadline = time.monotonic() + 90
        while time.monotonic() < online_deadline:
            if runner_proc.poll() is not None:
                process_log = (
                    runner_process_log.read_text(encoding="utf-8")
                    if runner_process_log.exists()
                    else ""
                )
                pytest.fail(
                    f"runner exited (code={runner_proc.returncode}) before coming online:\n"
                    f"stdout: {runner_log.read_text(encoding='utf-8')[-1500:]}\n"
                    f"process log: {process_log[-1500:]}"
                )
            try:
                resp = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                if resp.status_code == 200 and resp.json().get("online") is True:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        else:
            pytest.fail(
                "runner never came online:\n"
                f"server: {server_log.read_text(encoding='utf-8')[-1500:]}\n"
                f"runner: {runner_log.read_text(encoding='utf-8')[-1500:]}"
            )

        # Sit idle with no client attached; the watchdog should reap the runner.
        reap_deadline = time.monotonic() + idle_timeout_s * 4 + 30
        while time.monotonic() < reap_deadline:
            if runner_proc.poll() is not None:
                break
            time.sleep(1)
        else:
            pytest.fail(
                f"runner still alive after > {idle_timeout_s * 4 + 30}s idle "
                f"(configured window {idle_timeout_s}s)"
            )

        runner_output = (
            runner_process_log.read_text(encoding="utf-8") if runner_process_log.exists() else ""
        )
        assert "idle timeout reached" in runner_output, (
            f"runner exited but not via the idle watchdog:\n{runner_output[-3000:]}"
        )

        # The server now sees the runner offline: the dead session a user finds.
        status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=5)
        assert status.status_code == 200
        assert status.json().get("online") is False
    finally:
        for proc in (runner_proc, server_proc):
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        server_handle.close()
        runner_handle.close()
