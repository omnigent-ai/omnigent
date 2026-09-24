"""Exercise real server/runner recovery after deleting the runner launch cwd.

The runner keeps a separate valid ``OMNIGENT_RUNNER_WORKSPACE``. After its
launch directory is removed, binding and ensuring a Claude terminal must
still succeed. A stub Claude binary prevents provider access; tmux and the
loopback server and runner processes are real."""

from __future__ import annotations

import contextlib
import io
import json
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Ignore ambient proxies for loopback traffic.
_http = httpx.Client(trust_env=False)

_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

from omnigent.runner.identity import (  # noqa: E402
    OMNIGENT_INTERNAL_WS_ORIGIN,
    token_bound_runner_id,
)

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 1.0
# Include the route's runner-connect wait.
_ENSURE_TIMEOUT_S = 90.0

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="claude-native terminals run inside tmux; tmux not installed",
)


def _find_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
    """Build an isolated loopback subprocess environment from *extra*."""
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    for name in list(env):
        if name.startswith("OMNIGENT") or name == "RUNNER_SERVER_URL":
            env.pop(name, None)
    env.update(extra)
    return env


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    """Best-effort SIGTERM -> SIGKILL teardown for a spawned process."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
    """Poll *url* until it returns 200 or *deadline* (monotonic) passes."""
    last = "not polled"
    while time.monotonic() < deadline:
        try:
            if _http.get(url, timeout=2.0).status_code == 200:
                return
            last = "non-200"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(_POLL_S)
    raise AssertionError(f"{url} never became healthy: {last}")


def _create_claude_native_session(base_url: str) -> tuple[str, str]:
    """Create the same session-scoped Claude wrapper agent as ``omnigent claude``."""
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_claude_agent_spec(Path(tmp)).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Exercise the wrapper's compatibility translation path.
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    create = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={
            "bundle": (
                "claude-native-ui.tar.gz",
                buf.getvalue(),
                "application/gzip",
            )
        },
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        timeout=30.0,
    )
    create.raise_for_status()
    body = create.json()
    return str(body["session_id"]), str(body["agent_id"])


def test_native_claude_recreate_survives_removed_launch_cwd(tmp_path: Path) -> None:
    """Recreate Claude after deleting the runner launch cwd but preserving its workspace."""
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "chat.db"
    database_uri = f"sqlite:///{db_path}"

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    launch_cwd = tmp_path / "runner-launch-cwd"
    launch_cwd.mkdir()
    runner_home = tmp_path / "home"
    runner_home.mkdir()
    server_home = tmp_path / "server-home"
    server_home.mkdir()
    # Keep tmux's AF_UNIX socket below the platform path limit.
    tmp_parent = "/tmp" if os.path.isdir("/tmp") and os.access("/tmp", os.W_OK) else None
    runtime_tmp = Path(tempfile.mkdtemp(prefix="oterm_", dir=tmp_parent))
    harness_tmp = tmp_path / "harness"
    harness_tmp.mkdir()
    runtime_env = {
        "TMPDIR": str(runtime_tmp),
        "OMNIGENT_HARNESS_TMP_PARENT": str(harness_tmp),
    }

    # Prevent accidental provider access through an installed Claude CLI.
    stub_bin = tmp_path / "bin"
    stub_bin.mkdir()
    stub = stub_bin / "claude"
    stub.write_text("#!/bin/sh\nexec sleep 600\n")
    stub.chmod(0o755)

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    server_log = (tmp_path / "server.log").open("w")
    runner_log = (tmp_path / "runner.log").open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    try:
        server_proc = subprocess.Popen(
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
                database_uri,
                "--artifact-location",
                str(tmp_path / "artifacts"),
            ],
            env=_localhost_env(
                {
                    **runtime_env,
                    "HOME": str(server_home),
                    "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token,
                }
            ),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)

        # Give the runner a deletable process cwd and a separate valid workspace.
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            cwd=str(launch_cwd),
            env=_localhost_env(
                {
                    **runtime_env,
                    "OMNIGENT_RUNNER_ID": runner_id,
                    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
                    "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
                    "RUNNER_SERVER_URL": base_url,
                    "OMNIGENT_RUNNER_WORKSPACE": str(workspace),
                    "HOME": str(runner_home),
                    "PATH": f"{stub_bin}{os.pathsep}{os.environ.get('PATH', '')}",
                }
            ),
            stdout=runner_log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            try:
                status = _http.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2.0)
                if status.status_code == 200 and status.json().get("online") is True:
                    online = True
                    break
            except httpx.HTTPError:
                # Runner is still starting.
                pass
            time.sleep(_POLL_S)
        assert online, (
            f"runner never came online; log:\n{(tmp_path / 'runner.log').read_text()[-3000:]}"
        )

        session_id, _agent_id = _create_claude_native_session(base_url)

        # Delete cwd before binding triggers terminal auto-creation.
        shutil.rmtree(launch_cwd)

        # Binding follows the web/CLI launch path.
        _http.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=30.0,
        ).raise_for_status()

        # Send the native wrapper's terminal ensure request.
        ensure = _http.post(
            f"{base_url}/v1/sessions/{session_id}/resources/terminals",
            json={
                "terminal": "claude",
                "session_key": "main",
                "ensure_native_terminal": True,
            },
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            timeout=_ENSURE_TIMEOUT_S,
        )

        log_dir = runner_home / ".omnigent" / "logs" / "runner"

        def _runner_log_text() -> str:
            """Union the stdout capture and any rotated runner log files."""
            texts: list[str] = []
            with contextlib.suppress(OSError):
                texts.append((tmp_path / "runner.log").read_text())
            for candidate in log_dir.glob("runner-*.log"):
                try:
                    texts.append(candidate.read_text())
                except OSError:
                    continue
            return "\n".join(texts)

        # Attribute a failure to the deleted-cwd regression after logs flush.
        if ensure.status_code >= 400:
            runner_log_text = ""
            fault_deadline = time.monotonic() + 15.0
            while time.monotonic() < fault_deadline:
                runner_log_text = _runner_log_text()
                if "FileNotFoundError" in runner_log_text and "getcwd" in runner_log_text:
                    break
                time.sleep(0.5)
            assert "FileNotFoundError" in runner_log_text and "getcwd" in runner_log_text, (
                f"recreation failed ({ensure.status_code}: {ensure.text}) but not via "
                f"the reported removed-cwd read; runner log tail:\n"
                f"{runner_log_text[-3000:]}"
            )

        assert ensure.status_code < 400, (
            "native Claude terminal recreation must not depend on the runner's "
            "removed process cwd when OMNIGENT_RUNNER_WORKSPACE is valid; got "
            f"{ensure.status_code}: {ensure.text}\n"
            f"runner log tail:\n{_runner_log_text()[-3000:]}"
        )
    finally:
        _terminate(runner_proc)
        _terminate(server_proc)
        server_log.close()
        runner_log.close()
        shutil.rmtree(runtime_tmp, ignore_errors=True)
