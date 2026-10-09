"""Start a real ``omnigent server`` against the crash-looping stub cluster.

Shared by the HTTP and browser guards for the workspace-prep crash loop: the
server runs as a subprocess with ``sandbox.provider: kubernetes`` and the stub
SDK from :mod:`tests.e2e._k8s_crashloop_stub_sdk` on its ``PYTHONPATH``.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml

from tests.e2e._k8s_crashloop_stub_sdk import write_stub_sdk

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENT_NAME = "sdk-chat-builtin"

_HEALTH_TIMEOUT_S = 180.0
_POLL_INTERVAL_S = 0.5


def write_server_config(tmp_path: Path, port: int, pod_ready_timeout_s: int) -> Path:
    """Write a server config enabling the kubernetes sandbox provider."""
    kubeconfig = tmp_path / "kubeconfig"
    kubeconfig.write_text("")
    config_path = tmp_path / "server-config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "sandbox": {
                    "server_url": f"http://127.0.0.1:{port}",
                    "provider": "kubernetes",
                    "kubernetes": {
                        "image": "ghcr.io/omnigent-ai/omnigent-host:e2e",
                        "namespace": "omnigent-sandboxes",
                        "in_cluster": False,
                        "kubeconfig": str(kubeconfig),
                        "pod_ready_timeout_s": pod_ready_timeout_s,
                    },
                }
            }
        )
    )
    return config_path


def spawn_server(
    tmp_path: Path, port: int, pod_ready_timeout_s: int
) -> tuple[subprocess.Popen[bytes], Path]:
    """Start a real ``omnigent server`` subprocess wired to the stub SDK."""
    stub_root = tmp_path / "k8s_stub"
    write_stub_sdk(stub_root)
    # A server spawned from inside a runner must not inherit that runner's identity.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("OMNIGENT_RUNNER_", "OMNIGENT_HOST_"))
    }
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(
                [
                    str(stub_root),
                    str(REPO_ROOT),
                    str(REPO_ROOT / "sdks" / "python-client"),
                    str(REPO_ROOT / "sdks" / "ui"),
                    os.environ.get("PYTHONPATH", ""),
                ]
            ),
            "OPENAI_API_KEY": "unused-no-turn-runs",
            "OMNIGENT_BUILTIN_AGENT_DIRS": str(
                REPO_ROOT / "tests" / "resources" / "agents" / f"{AGENT_NAME}.yaml"
            ),
        }
    )
    log_path = tmp_path / "server.log"
    # The child owns its own copy of the log fd; closing ours after spawn is safe.
    with open(log_path, "w") as log_handle:
        proc = subprocess.Popen(
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
                f"sqlite:///{tmp_path / 'e2e.db'}",
                "--artifact-location",
                str(tmp_path / "artifacts"),
                "--config",
                str(write_server_config(tmp_path, port, pod_ready_timeout_s)),
            ],
            env=env,
            cwd=str(REPO_ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return proc, log_path


def wait_for_health(proc: subprocess.Popen[bytes], base_url: str, log_path: Path) -> None:
    """Wait for ``/health``, failing with the server log if the process dies."""
    deadline = time.monotonic() + _HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            pytest.fail(
                f"server exited (code {proc.returncode}) before serving /health:\n"
                f"{log_path.read_text()[-2000:]}"
            )
        try:
            if httpx.get(f"{base_url}/health", timeout=2.0).status_code == 200:
                return
        except httpx.HTTPError:
            pass  # not listening yet; keep polling until the deadline
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"server did not become healthy:\n{log_path.read_text()[-2000:]}")


def terminate(proc: subprocess.Popen[bytes]) -> None:
    """Stop the server's own process group (it was spawned with ``start_new_session``)."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)
