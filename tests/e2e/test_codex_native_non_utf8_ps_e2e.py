"""E2E regression test: codex-native launch survives non-UTF-8 ``ps`` output.

macOS ``ps`` passes raw process argv bytes through, so a running app whose
argv is not valid UTF-8 (the reported trigger: UltraEdit) makes
``ps -axww -o pid=,pgid=,command=`` emit invalid UTF-8. The codex-native
launch path's stale-process reaper decodes that listing strictly and dies
with ``UnicodeDecodeError``, so the codex terminal never starts and the
session fails. Linux procps sanitizes argv bytes to ``?``, so this test
shims ``ps`` on the host daemon's PATH to reproduce the macOS-shaped byte
stream (the real listing plus one raw invalid-UTF-8 line), then drives the
real journey: connect a host -> create a ``codex-native-ui`` session ->
the codex terminal must still register.

Run::

    OMNIGENT_E2E_CODEX_NATIVE=1 \\
    .venv/bin/python -m pytest tests/e2e/test_codex_native_non_utf8_ps_e2e.py -v
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from omnigent.entities.session_resources import terminal_resource_id
from omnigent.native.native_coding_agents import CODEX_NATIVE_AGENT_NAME
from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.helpers import POLL_INTERVAL_S

pytestmark = pytest.mark.skipif(
    os.environ.get("OMNIGENT_E2E_CODEX_NATIVE") != "1" or shutil.which("codex") is None,
    reason=(
        "codex-native non-UTF-8 ps e2e needs `codex` on PATH and "
        "OMNIGENT_E2E_CODEX_NATIVE=1 to run"
    ),
)

_LAUNCH_TIMEOUT_S = 180.0

# One macOS-shaped ps line whose command column carries raw invalid UTF-8,
# mimicking the reporter's UltraEdit process. It must not contain
# "app-server" or a bridge state dir so a fixed reaper never targets it.
_RAW_NON_UTF8_PS_LINE = (
    b"  99999 99999 /Applications/UltraEdit.app/Contents/MacOS/UltraEdit --profile\xff\xfe\n"
)


def _write_ps_shim(shim_dir: Path) -> None:
    """
    Install a ``ps`` shim that appends a raw non-UTF-8 line to real output.

    :param shim_dir: Directory to prepend to the daemon's PATH.
    """
    real_ps = shutil.which("ps")
    assert real_ps, "real `ps` not found on PATH"
    raw_line = shim_dir / "rawline.bin"
    raw_line.write_bytes(_RAW_NON_UTF8_PS_LINE)
    shim = shim_dir / "ps"
    shim.write_text(
        f'#!/bin/sh\n"{real_ps}" "$@"\ncat "{raw_line}"\n',
        encoding="utf-8",
    )
    shim.chmod(0o755)


def _spawn_host_daemon(
    *,
    home_dir: Path,
    shim_dir: Path,
    log_path: Path,
    live_server: str,
) -> subprocess.Popen[bytes]:
    """
    Spawn an ``omnigent host`` daemon whose PATH resolves the ``ps`` shim.

    :param home_dir: Isolated home for this test's daemon and runner state.
    :param shim_dir: Directory containing the ``ps`` shim.
    :param log_path: File that captures the daemon's output.
    :param live_server: Test server base URL.
    :returns: The spawned daemon subprocess handle.
    """
    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    env["PATH"] = f"{shim_dir}{os.pathsep}{env['PATH']}"
    # Absolute sdks paths: the daemon-spawned runner (zygote) runs from the
    # session workspace, where a relative ``sdks/python-client`` would not
    # resolve ``omnigent_client``.
    py_paths = [
        str(repo_root),
        str(repo_root / "sdks" / "python-client"),
        str(repo_root / "sdks" / "ui"),
    ]
    if env.get("PYTHONPATH"):
        py_paths.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(py_paths)
    with open(log_path, "w") as log_fh:
        return subprocess.Popen(
            [
                runner_executable(),
                "-m",
                "omnigent.host._daemon_entry",
                "--server",
                live_server,
            ],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=log_fh,
            stderr=log_fh,
        )


def _wait_for_host_connection(
    proc: subprocess.Popen[bytes],
    log_path: Path,
    timeout: float = 45.0,
) -> None:
    """Wait until the daemon logs that its tunnel connected."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"Host daemon exited with {proc.returncode}:\n"
                f"{log_path.read_text(encoding='utf-8', errors='replace')}"
            )
        if "✓ Connected as" in log_path.read_text(encoding="utf-8", errors="replace"):
            return
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"Host daemon did not connect within {timeout}s")


def _online_host_id(client: httpx.Client, timeout: float = 45.0) -> str:
    """Poll ``GET /v1/hosts`` until one host is online and return its id."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        resp = client.get("/v1/hosts")
        if resp.status_code == 200:
            online = [h for h in resp.json().get("hosts", []) if h["status"] == "online"]
            if online:
                return str(online[0]["host_id"])
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"No host came online within {timeout}s")


def _codex_native_agent_id(client: httpx.Client) -> str:
    """Return the durable id of the auto-registered ``codex-native-ui``."""
    resp = client.get("/v1/agents")
    resp.raise_for_status()
    for agent in resp.json()["data"]:
        if agent["name"] == CODEX_NATIVE_AGENT_NAME:
            return str(agent["id"])
    raise AssertionError(f"{CODEX_NATIVE_AGENT_NAME!r} not registered on the server")


def _runner_log_excerpt(home_dir: Path, limit: int = 4000) -> str:
    """Return the tail of every runner log under *home_dir* for diagnostics."""
    log_dir = home_dir / ".omnigent" / "logs" / "runner"
    if not log_dir.is_dir():
        return f"(no runner logs under {log_dir})"
    chunks: list[str] = []
    for log_file in sorted(log_dir.glob("*.log")):
        text = log_file.read_text(encoding="utf-8", errors="replace")
        chunks.append(f"--- {log_file.name} ---\n{text[-limit:]}")
    return "\n".join(chunks) or f"(no .log files under {log_dir})"


def test_codex_native_launch_survives_non_utf8_ps_output(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    """
    A codex-native session still launches while ``ps`` emits invalid UTF-8.

    Journey: a process with non-UTF-8 argv is visible in ``ps`` (macOS
    UltraEdit, stood in by the PATH shim) -> connect a host -> create a
    codex-native-ui session -> the codex terminal registers instead of the
    launch crashing with ``UnicodeDecodeError`` in the process reaper.
    """
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _write_ps_shim(shim_dir)
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    workspace = tmp_path / "codex_ws"
    workspace.mkdir()

    daemon_log = tmp_path / "host-daemon.log"
    daemon = _spawn_host_daemon(
        home_dir=home_dir,
        shim_dir=shim_dir,
        log_path=daemon_log,
        live_server=live_server,
    )
    session_id: str | None = None
    try:
        _wait_for_host_connection(daemon, daemon_log)
        host_id = _online_host_id(http_client)
        agent_id = _codex_native_agent_id(http_client)

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

        expected = terminal_resource_id("codex", "main")
        deadline = time.monotonic() + _LAUNCH_TIMEOUT_S
        last_seen: list[object] = []
        while time.monotonic() < deadline:
            resp = http_client.get(f"/v1/sessions/{session_id}/resources")
            if resp.status_code == 200:
                data = resp.json().get("data", [])
                last_seen = [r.get("id") for r in data]
                if any(r.get("id") == expected and r.get("type") == "terminal" for r in data):
                    return
            time.sleep(POLL_INTERVAL_S)
        raise AssertionError(
            f"codex terminal {expected!r} never registered for {session_id} within "
            f"{_LAUNCH_TIMEOUT_S:.0f}s while ps output contained non-UTF-8 bytes; "
            f"saw resources {last_seen!r}.\nRunner log tail:\n"
            f"{_runner_log_excerpt(home_dir)}"
        )
    finally:
        if daemon.poll() is None:
            daemon.terminate()
            try:
                daemon.wait(timeout=15)
            except subprocess.TimeoutExpired:
                daemon.kill()
                daemon.wait(timeout=15)
        if session_id is not None:
            with httpx.Client(base_url=live_server) as cleanup:
                with contextlib.suppress(httpx.HTTPError):
                    cleanup.delete(f"/v1/sessions/{session_id}", timeout=30.0)
