"""
End-to-end guard: a crash-looping workspace-prep init container must fail the
managed Kubernetes launch fast, with the init container's log tail attached.

User journey (operator + user), from the bug report:

1. An operator configures ``sandbox.provider: kubernetes`` on the server,
   pointing at a cluster whose sandbox namespace cannot reach the clone host
   (a default-deny NetworkPolicy is enough).
2. A user creates a managed session with a repository workspace
   (``POST /v1/sessions`` with ``host_type: "managed"`` and a repo-URL
   ``workspace``), which makes the server's Kubernetes launcher submit a Job
   and wait for its Pod.
3. ``git clone`` in the ``workspace-prep`` init container fails immediately
   and the kubelet restarts it: the Pod sits in phase ``Pending`` with the
   init container in ``CrashLoopBackOff`` — it will never come up.
4. The user watches the session's sandbox launch progress. Expected: the
   launch fails fast, names the crash-looping container, and carries a tail
   of its log (the clone error). Observed (bug): the launch polls the full
   ``pod_ready_timeout_s`` (90s by default) and the failure carries only Pod
   events, never the log tail.

The apiserver is unreachable from the test environment, so a stub
``kubernetes`` package on the server subprocess's PYTHONPATH stands in for
the cluster, replaying exactly what a real apiserver reports for this state
(``init_container_statuses[0].state.waiting.reason == "CrashLoopBackOff"``,
Pod ``Pending``, kubelet ``BackOff`` events, the clone error in the init
container's log). Everything else is real: the server process, its config
parsing, the managed-session HTTP journey, the launcher's start wait, and
the failure-message builder.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml

from tests.e2e._k8s_crashloop_stub_sdk import CLONE_ERROR_LINE as _CLONE_ERROR_LINE
from tests.e2e._k8s_crashloop_stub_sdk import STUB_FILES as _STUB_FILES

_REPO_ROOT = Path(__file__).resolve().parents[2]

_HEALTH_TIMEOUT_S = 180.0
_POLL_INTERVAL_S = 0.5

# The repository workspace the user asks for — the clone the init container
# fails on.
_REPO_URL = "https://github.com/omnigent-ai/omnigent"

# Pod-ready budget for the fail-fast test: generous enough that a fail-fast
# well under it is unambiguous, small enough that the buggy
# poll-to-the-deadline path doesn't stall CI for the default 90s.
_FAILFAST_POD_READY_TIMEOUT_S = 45

# A crash-loop detected at the first poll after the stub enters
# CrashLoopBackOff (~3s in) fails the launch within seconds; the buggy path
# cannot fail before the 45s deadline. 25s splits the two with wide margins.
_FAILFAST_MAX_S = 25.0

# Pod-ready budget for the log-tail test: the message-content assertion is
# deadline-independent, so keep the buggy run short.
_LOG_TAIL_POD_READY_TIMEOUT_S = 10


def _find_free_port() -> int:
    """Bind port 0 and return the assigned free port."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _write_stub_sdk(tmp_path: Path) -> Path:
    """Materialize the stub ``kubernetes`` package; return its sys.path root."""
    root = tmp_path / "k8s_stub"
    for rel, source in _STUB_FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    return root


def _write_server_config(tmp_path: Path, port: int, pod_ready_timeout_s: int) -> Path:
    """Write a server config enabling the kubernetes sandbox provider."""
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
                        "kubeconfig": str(tmp_path / "kubeconfig"),
                        "pod_ready_timeout_s": pod_ready_timeout_s,
                    },
                }
            }
        )
    )
    (tmp_path / "kubeconfig").write_text("")
    return config_path


def _spawn_server(
    tmp_path: Path, config_path: Path, port: int
) -> tuple[subprocess.Popen[bytes], Path]:
    """Start a real ``omnigent server`` subprocess wired to the stub SDK."""
    stub_root = _write_stub_sdk(tmp_path)
    pythonpath = os.pathsep.join(
        [
            str(stub_root),
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            os.environ.get("PYTHONPATH", ""),
        ]
    )
    env = {
        **os.environ,
        "PYTHONPATH": pythonpath,
        "OPENAI_API_KEY": "unused-no-turn-runs",
        "OMNIGENT_BUILTIN_AGENT_DIRS": str(
            _REPO_ROOT / "tests" / "resources" / "agents" / "sdk-chat-builtin.yaml"
        ),
    }
    log_path = tmp_path / "server.log"
    log_handle = open(log_path, "w")  # noqa: SIM115 — lives for the Popen's lifetime
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{tmp_path / 'e2e.db'}",
            "--artifact-location",
            str(tmp_path / "artifacts"),
            "--config",
            str(config_path),
        ],
        env=env,
        cwd=str(_REPO_ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    return proc, log_path


def _wait_for_health(proc: subprocess.Popen[bytes], base_url: str, log_path: Path) -> None:
    """Wait for /health, failing with the server log if the process dies."""
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
            pass
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"server did not become healthy:\n{log_path.read_text()[-2000:]}")


def _create_managed_repo_session(base_url: str) -> str:
    """Drive the user journey: create a managed session with a repo workspace."""
    info = httpx.get(f"{base_url}/v1/info", timeout=10.0).json()
    assert info.get("managed_sandboxes_enabled") is True
    agents = httpx.get(f"{base_url}/v1/agents", timeout=10.0).json()["data"]
    assert agents, "no agents registered on the server to bind a session to"
    response = httpx.post(
        f"{base_url}/v1/sessions",
        json={
            "agent_id": agents[0]["id"],
            "host_type": "managed",
            "workspace": _REPO_URL,
        },
        timeout=120.0,
    )
    assert response.status_code == 201, (
        f"managed session create failed: HTTP {response.status_code}: {response.text[:500]}"
    )
    session_id: str = response.json()["id"]
    return session_id


def _await_failed_launch(
    base_url: str, session_id: str, budget_s: float, log_path: Path
) -> tuple[float, str]:
    """Poll the session snapshot until its sandbox launch fails.

    :returns: ``(elapsed_s, error)`` — seconds from call start until the
        snapshot's ``sandbox_status`` reported stage ``failed``, and the
        failure detail shown to the user.
    """
    start = time.monotonic()
    deadline = start + budget_s
    stage = None
    while time.monotonic() < deadline:
        snapshot = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
        status = snapshot.get("sandbox_status") or {}
        stage = status.get("stage")
        if stage == "failed":
            return time.monotonic() - start, str(status.get("error") or "")
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(
        f"sandbox launch never reported failure within {budget_s}s "
        f"(last stage {stage!r}):\n{log_path.read_text()[-3000:]}"
    )


def test_init_crashloop_fails_fast_before_pod_ready_deadline(tmp_path: Path) -> None:
    """A crash-looping init container must fail the launch fast, not at the deadline.

    The stub cluster parks ``workspace-prep`` in ``CrashLoopBackOff`` ~3s
    after the Job is submitted; the kubelet will never bring the Pod up. The
    start wait is expected to detect that terminal state and fail within
    seconds. The bug: ``_terminal_failure`` never reads
    ``init_container_statuses`` outside phase ``Failed``, so the launch
    burns the whole ``pod_ready_timeout_s`` (90s by default) before failing.
    """
    port = _find_free_port()
    config_path = _write_server_config(tmp_path, port, _FAILFAST_POD_READY_TIMEOUT_S)
    proc, log_path = _spawn_server(tmp_path, config_path, port)
    try:
        base_url = f"http://127.0.0.1:{port}"
        _wait_for_health(proc, base_url, log_path)
        session_id = _create_managed_repo_session(base_url)
        elapsed, error = _await_failed_launch(
            base_url, session_id, _FAILFAST_POD_READY_TIMEOUT_S + 45.0, log_path
        )
    finally:
        proc.kill()
        proc.wait(timeout=30)

    assert elapsed < _FAILFAST_MAX_S, (
        f"launch failure took {elapsed:.1f}s — the crash-looping workspace-prep "
        f"init container was not fail-fast detected and the start wait polled to "
        f"its {_FAILFAST_POD_READY_TIMEOUT_S}s pod-ready deadline (90s in a "
        f"default deployment); error: {error[:500]}"
    )


def test_init_crashloop_failure_carries_init_container_log_tail(tmp_path: Path) -> None:
    """The launch failure must carry the init container's log tail.

    The module docstring promises the launch error carries "a tail of the
    failed container's log (e.g. the ``git clone`` error from the init
    container)", and the stub cluster serves exactly that log. The bug: the
    crash-loop is never attributed to the init container, the poll times
    out, and the timeout diagnostics attach Pod events but never fetch the
    container log — leaving the user without the actual clone error.
    """
    port = _find_free_port()
    config_path = _write_server_config(tmp_path, port, _LOG_TAIL_POD_READY_TIMEOUT_S)
    proc, log_path = _spawn_server(tmp_path, config_path, port)
    try:
        base_url = f"http://127.0.0.1:{port}"
        _wait_for_health(proc, base_url, log_path)
        session_id = _create_managed_repo_session(base_url)
        _, error = _await_failed_launch(
            base_url, session_id, _LOG_TAIL_POD_READY_TIMEOUT_S + 45.0, log_path
        )
    finally:
        proc.kill()
        proc.wait(timeout=30)

    assert "workspace-prep" in error, (
        f"launch failure does not name the failed init container: {error[:800]}"
    )
    assert _CLONE_ERROR_LINE in error, (
        "launch failure dropped the workspace-prep log tail — the user never "
        "sees the git clone error the init container printed (the apiserver "
        f"served it via read_namespaced_pod_log); error: {error[:800]}"
    )
