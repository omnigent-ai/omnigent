"""Helper: spawn a dedicated Omnigent server behind a configured base path.

Mirrors the shared ``live_server`` spawn (``tests/e2e_ui/conftest.py``) but
sets ``OMNIGENT_WEB_BASE_PATH`` and needs no mock-LLM-backed runner — the
routing/asset/WebSocket-resolution behavior under test needs no agent turn.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx

from tests.e2e_ui.conftest import (
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _find_free_port,
)


@dataclass
class BasePathServer:
    """A running server mounted behind ``base_path``.

    :param base_url: Loopback origin (``http://127.0.0.1:<port>``), unprefixed.
    :param base_path: The configured prefix, e.g. ``"/proxy/6767"``.
    """

    base_url: str
    base_path: str

    @property
    def prefixed_url(self) -> str:
        """``base_url`` + ``base_path`` — where the SPA is actually mounted."""
        return f"{self.base_url}{self.base_path}"


def _terminate(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def spawn_base_path_server(server_tmp: Path, base_path: str) -> Iterator[BasePathServer]:
    """Spawn ``omnigent server --base-path <base_path>``; yield a handle.

    No runner is bound and no mock LLM is wired: nothing under test drives an
    agent turn, only page/asset/WebSocket routing under the configured prefix.

    :param server_tmp: A per-test temp dir (``tmp_path_factory.mktemp(...)``).
    :param base_path: Prefix to configure, e.g. ``"/proxy/6767"``.
    :yields: A :class:`BasePathServer` handle.
    """
    port = _find_free_port()
    log_path = server_tmp / "server.log"
    db_path = server_tmp / "test.db"
    artifact_dir = server_tmp / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    agent_yaml_path = server_tmp / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML)

    base_url = f"http://127.0.0.1:{port}"
    pythonpath = f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"

    server_env = {
        **os.environ,
        "PYTHONPATH": pythonpath,
        "OMNIGENT_WEB_BASE_PATH": base_path,
    }

    log_handle = open(log_path, "w")  # noqa: SIM115 — lives for the Popen; closed in finally
    proc = subprocess.Popen(
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
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
            "--agent",
            str(agent_yaml_path),
        ],
        env=server_env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    try:
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        ready = False
        last_error = "not polled yet"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                last_error = f"server exited early with code {proc.returncode}"
                break
            try:
                if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                    ready = True
                    break
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(_HEALTH_POLL_INTERVAL_S)
        if not ready:
            log_handle.flush()
            log_text = log_path.read_text() if log_path.exists() else ""
            raise RuntimeError(
                f"base-path server not healthy within {_HEALTH_TIMEOUT_S:.0f}s on "
                f"{base_url} (last_error={last_error}).\n{log_text[-3000:]}"
            )

        yield BasePathServer(base_url=base_url, base_path=base_path)
    finally:
        _terminate(proc)
        log_handle.close()
