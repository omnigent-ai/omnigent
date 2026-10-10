"""Real-process regressions for Windows degraded-mode startup."""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

import omnigent.inner.os_env as os_env_module
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.process_logging import PROCESS_LOG_FILE_ENV_VAR
from tests._helpers.compat import apply_runner_env, runner_executable
from tests.e2e.conftest import POLL_INTERVAL_S

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason=(
        "POSIX stand-in for the Windows-only failure states; on native "
        "Windows the journeys reproduce directly without the emulation"
    ),
)

_REPO_ROOT = Path(__file__).resolve().parents[2]

_ENCODE_CRASH_MARKER = "can't encode"

# Emulate Windows' stdio choice before entering the real daemon process.
_WINDOWS_ANSI_STDIO_BOOTSTRAP = """\
import os, sys

if os.environ.get("PYTHONUTF8") != "1" and not os.environ.get("PYTHONIOENCODING"):
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if _reconfigure is not None:
            _reconfigure(encoding="cp1252")

sys.argv = ["omnigent-host-daemon", "--server", sys.argv[1]]
from omnigent.host._daemon_entry import main

main()
"""


def _read_text(path: Path) -> str:
    """Read a log file leniently (it may hold cp1252 bytes), or ``""``."""
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8", errors="replace")


def _crash_lines(*logs: str) -> list[str]:
    """Collect log lines carrying the legacy-codepage encode crash."""
    lines: list[str] = []
    for log in logs:
        lines.extend(line for line in log.splitlines() if _ENCODE_CRASH_MARKER in line)
    return lines


def _host_online(client: httpx.Client, host_id: str) -> bool:
    """Return True when *host_id* reports online via ``GET /v1/hosts``."""
    try:
        resp = client.get("/v1/hosts")
    except httpx.HTTPError:
        return False
    if resp.status_code != 200:
        return False
    return any(
        host.get("host_id") == host_id and host.get("status") == "online"
        for host in resp.json().get("hosts", [])
    )


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    """SIGTERM then SIGKILL a subprocess, reaping it."""
    if proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def test_host_daemon_tunnel_survives_windows_ansi_stdio(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real host daemon must keep its tunnel with cp1252 stdio."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "omnigent-data"))
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    monkeypatch.delenv("PYTHONIOENCODING", raising=False)

    omni_dir = tmp_path / ".omnigent"
    omni_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(omni_dir))
    host_id = uuid.uuid4().hex
    host_name = f"e2e-cp1252-tunnel-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": host_name}},
            default_flow_style=False,
            sort_keys=True,
        )
    )

    from omnigent.cli import _build_host_daemon_env

    env = _build_host_daemon_env(server_url=live_server)
    env["PATH"] = os.pathsep.join((str(Path(runner_executable()).parent), "/usr/bin", "/bin"))
    daemon_log = tmp_path / "host-daemon.log"
    env[PROCESS_LOG_FILE_ENV_VAR] = str(daemon_log)
    # The daemon runs from tmp, so make worktree-relative import paths absolute.
    env["OMNIGENT_DATA_DIR"] = str(tmp_path / "omnigent-data")
    ambient_pythonpath = os.environ.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_REPO_ROOT)]
        + [
            entry if os.path.isabs(entry) else str(_REPO_ROOT / entry)
            for entry in ambient_pythonpath.split(os.pathsep)
            if entry
        ]
    )
    apply_runner_env(env)

    stdio_log = tmp_path / "host-daemon-stdio.log"
    with open(stdio_log, "wb") as stdio_fh:
        proc = subprocess.Popen(
            [
                runner_executable(),
                "-c",
                _WINDOWS_ANSI_STDIO_BOOTSTRAP,
                live_server,
            ],
            env=env,
            cwd=str(tmp_path),
            stdin=subprocess.DEVNULL,
            stdout=stdio_fh,
            stderr=stdio_fh,
        )

    try:
        deadline = time.monotonic() + 90.0
        # Keep observing after registration so the success print has run.
        survival_deadline: float | None = None
        while time.monotonic() < deadline:
            crash = _crash_lines(_read_text(daemon_log), _read_text(stdio_log))
            if crash:
                pytest.fail(
                    "legacy-ANSI (cp1252) stdio killed the host tunnel: the "
                    "daemon's own status print raised UnicodeEncodeError and "
                    "tore down the connection (reconnect loop). Offending "
                    "log lines:\n" + "\n".join(crash[:8])
                )
            if proc.poll() is not None:
                pytest.fail(
                    f"host daemon exited rc={proc.returncode} before the "
                    "tunnel was established:\n"
                    + _read_text(daemon_log)[-2000:]
                    + _read_text(stdio_log)[-2000:]
                )
            if survival_deadline is None:
                if _host_online(http_client, host_id):
                    survival_deadline = time.monotonic() + 8.0
            elif time.monotonic() >= survival_deadline:
                assert _host_online(http_client, host_id), (
                    "host dropped offline after registering (tunnel did not "
                    "hold):\n" + _read_text(daemon_log)[-2000:]
                )
                return
            time.sleep(POLL_INTERVAL_S)
        pytest.fail(
            f"host {host_name!r} never came online within 90s — daemon log:\n"
            + _read_text(daemon_log)[-2000:]
            + _read_text(stdio_log)[-2000:]
        )
    finally:
        _terminate(proc)


def test_os_tools_start_under_windows_config_delivery_without_sandbox(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OS tools must start through Windows config delivery without a sandbox."""
    monkeypatch.setattr(os_env_module, "IS_WINDOWS", True)

    spec = OSEnvSpec(
        type="caller_process",
        cwd=str(tmp_path),
        sandbox=OSEnvSandboxSpec(type="none"),
    )
    env = os_env_module.create_os_environment(spec)
    assert env is not None, "factory returned no OS environment"
    assert env.sandbox.active is False, (
        "precondition: the sandbox must be inactive (Windows never has an active one)"
    )

    marker = f"omni-degraded-os-tools-{uuid.uuid4().hex[:8]}"
    try:
        shell_result = asyncio.run(env.shell(f"echo {marker}"))
        read_result = asyncio.run(env.read("does-not-exist.txt"))
    finally:
        env.close()

    assert isinstance(shell_result, dict)
    assert not shell_result.get("error"), (
        f"sys_os_shell returned an error payload instead of running the command: {shell_result!r}"
    )
    assert shell_result.get("exit_code") == 0, f"unexpected result: {shell_result!r}"
    assert marker in (shell_result.get("stdout") or ""), (
        f"command output missing: {shell_result!r}"
    )
    assert isinstance(read_result, dict)
    assert "os_env helper failed" not in (read_result.get("error") or ""), (
        f"helper never started for the read op: {read_result!r}"
    )
