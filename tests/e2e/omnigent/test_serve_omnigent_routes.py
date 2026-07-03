"""Phase 3 integration-code test -- ``omnigent server --agent`` routing (mock LLM).

Migrated to mock LLM: the test only boots the server and probes
HTTP routes -- no LLM calls are made, so mock credentials suffice.

**What breaks if this fails:**
- The Omnigent mode dispatch site at ``_serve_agent`` stops calling into
  omnigent and falls back to the legacy ``create_app``.
- The shim's ``_omnigent_register_yaml_bundle`` stops registering
  the synthesized bundle with Omnigent' ``AgentStore``.
- The shim's YAML translation pipeline regresses.
"""

from __future__ import annotations

import signal
import socket
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.e2e.omnigent._snapshot import compare_snapshot

_YAML_RELPATH = ("tests", "resources", "examples", "hello_world.yaml")

_SERVE_BOOT_TIMEOUT = 30.0

_HTTP_TIMEOUT = 10.0

_POLL_INTERVAL_S = 0.3


@contextmanager
def _omnigent_serve_omnigent(
    *,
    omnigent_python: Path,
    yaml_path: Path,
    port: int,
    env: dict[str, str],
    cwd: Path,
) -> Generator[subprocess.Popen[str]]:
    """Spawn ``omnigent server --agent <yaml> --port <port>``."""
    proc = subprocess.Popen(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "server",
            "--agent",
            str(yaml_path),
            "--port",
            str(port),
        ],
        env=env,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        yield proc
    finally:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def _wait_for_health(
    port: int,
    *,
    timeout: float,
    proc: subprocess.Popen[str],
) -> None:
    """Poll Omnigent' ``/health`` until the server responds 200."""
    deadline = time.monotonic() + timeout
    last_error: str | None = None
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            output = proc.stdout.read() if proc.stdout is not None else "<no output>"
            raise AssertionError(
                f"omnigent server --agent exited early with code "
                f"{proc.returncode} before /health became ready.\n\n"
                f"Server output:\n{output}"
            )
        try:
            resp = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2.0)
        except (httpx.ConnectError, httpx.ReadError) as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        else:
            if resp.status_code == 200:
                return
            last_error = f"HTTP {resp.status_code}"
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(
        f"omnigent server --agent did not respond on /health within "
        f"{timeout}s (last_error={last_error!r})."
    )


def _find_free_port() -> int:
    """Return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _gather_omnigent_observations(port: int) -> dict[str, Any]:
    """Capture structural observations proving the server is omnigent."""
    with httpx.Client(
        base_url=f"http://127.0.0.1:{port}",
        timeout=_HTTP_TIMEOUT,
    ) as client:
        health_resp = client.get("/health")
        agents_resp = client.get("/v1/agents")
        agents_body = agents_resp.json()
        agents_data = agents_body["data"]
        agent_names = [item["name"] for item in agents_data]
        incomplete_payload = {
            "agent_id": "missing",
            "input": [{"type": "message", "role": "user", "content": "hi"}],
            "stream": False,
            "background": False,
        }
        responses_resp = client.post(
            "/v1/responses",
            json=incomplete_payload,
        )
    return {
        "health_status": health_resp.status_code,
        "agents_list_status": agents_resp.status_code,
        "agents_has_hello_world": "hello_world" in agent_names,
        "responses_unknown_agent_status": responses_resp.status_code,
    }


def test_serve_omnigent_routes_to_omnigent(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
) -> None:
    """
    ``omnigent server --agent <yaml>`` boots an omnigent server
    with the YAML pre-registered. No LLM calls are made.
    """
    port = _find_free_port()
    yaml_path = omnigent_repo_root.joinpath(*_YAML_RELPATH)
    with _omnigent_serve_omnigent(
        omnigent_python=omnigent_python,
        yaml_path=yaml_path,
        port=port,
        env=mock_credentials_env,
        cwd=omnigent_repo_root,
    ) as proc:
        _wait_for_health(port, timeout=_SERVE_BOOT_TIMEOUT, proc=proc)
        observed = _gather_omnigent_observations(port)

    diffs = compare_snapshot("test_serve_omnigent_routes", observed)
    assert diffs == [], (
        "Snapshot mismatch for omnigent server --agent routing:\n"
        + "\n".join(diffs)
        + f"\n\nObserved: {observed!r}"
    )
