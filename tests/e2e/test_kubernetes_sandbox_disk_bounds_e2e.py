"""
End-to-end guard: Kubernetes managed sandboxes must carry a node-disk bound.

User journey (operator + user):

1. An operator configures ``sandbox.provider: kubernetes`` on the server with
   a ``resources`` block for the sandbox pods.
2. A user creates a managed session (``POST /v1/sessions`` with
   ``host_type: "managed"``), which makes the server's Kubernetes launcher
   submit a Job whose pod runs the sandbox host.
3. The submitted pod spec is all the scheduler and kubelet can enforce:
   without a ``sizeLimit`` on the writable-HOME ``emptyDir`` and without
   ``ephemeral-storage`` requests/limits, one busy sandbox can fill a node's
   root filesystem and trigger node-wide kubelet eviction of other sandboxes.

The apiserver is unreachable from the test environment, so a stub
``kubernetes`` package on the server subprocess's PYTHONPATH stands in for
the cluster and records the Job manifest handed to
``BatchV1Api.create_namespaced_job`` — the exact mapping the real client
would serialize onto the wire. Everything else is real: the server process,
its config parsing, the managed-session HTTP journey, and the launcher that
builds and submits the manifest.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml

from tests.e2e._k8s_stub_sdk import CAPTURE_ENV_VAR as _CAPTURE_ENV_VAR
from tests.e2e._k8s_stub_sdk import STUB_FILES as _STUB_FILES

_REPO_ROOT = Path(__file__).resolve().parents[2]

_HEALTH_TIMEOUT_S = 180.0
_MANIFEST_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 0.5


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


def _write_server_config(tmp_path: Path, port: int, resources: dict[str, dict[str, str]]) -> Path:
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
                        # The stub never reports a running pod; keep the
                        # pod-ready wait short so launch failure is fast. The
                        # Job manifest is captured before this wait.
                        "pod_ready_timeout_s": 1,
                        "resources": resources,
                    },
                }
            }
        )
    )
    (tmp_path / "kubeconfig").write_text("")
    return config_path


def _spawn_server(
    tmp_path: Path, config_path: Path, port: int, capture_path: Path
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
        _CAPTURE_ENV_VAR: str(capture_path),
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


def _create_managed_session(base_url: str) -> None:
    """Drive the user journey: create a session on a managed sandbox host."""
    info = httpx.get(f"{base_url}/v1/info", timeout=10.0).json()
    assert info.get("managed_sandboxes_enabled") is True
    agents = httpx.get(f"{base_url}/v1/agents", timeout=10.0).json()["data"]
    assert agents, "no agents registered on the server to bind a session to"
    response = httpx.post(
        f"{base_url}/v1/sessions",
        json={"agent_id": agents[0]["id"], "host_type": "managed"},
        timeout=120.0,
    )
    assert response.status_code == 201, (
        f"managed session create failed: HTTP {response.status_code}: {response.text[:500]}"
    )


def _await_submitted_job_manifest(capture_path: Path, log_path: Path) -> dict:
    """Return the Job manifest the launcher submitted to the apiserver."""
    deadline = time.monotonic() + _MANIFEST_TIMEOUT_S
    while time.monotonic() < deadline:
        if capture_path.exists():
            records = json.loads(capture_path.read_text())
            jobs = [r for r in records if r["call"] == "create_namespaced_job"]
            if jobs:
                return jobs[0]["manifest"]
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(
        "the launcher never submitted a Job for the managed session:\n"
        f"{log_path.read_text()[-3000:]}"
    )


def _pod_spec(manifest: dict) -> dict:
    """Return the pod spec of a Job manifest."""
    return manifest["spec"]["template"]["spec"]


def test_home_emptydir_carries_size_limit(tmp_path: Path) -> None:
    """The writable-HOME emptyDir must have a sizeLimit the kubelet enforces.

    Without one, the HOME volume is charged to the node's root filesystem
    with no per-pod bound, and nodefs pressure evicts unrelated pods.
    """
    port = _find_free_port()
    config_path = _write_server_config(
        tmp_path,
        port,
        {"requests": {"cpu": "500m", "memory": "1Gi"}, "limits": {"cpu": "2", "memory": "4Gi"}},
    )
    capture_path = tmp_path / "submitted.json"
    proc, log_path = _spawn_server(tmp_path, config_path, port, capture_path)
    try:
        base_url = f"http://127.0.0.1:{port}"
        _wait_for_health(proc, base_url, log_path)
        _create_managed_session(base_url)
        manifest = _await_submitted_job_manifest(capture_path, log_path)
    finally:
        proc.kill()
        proc.wait(timeout=30)

    volumes = _pod_spec(manifest)["volumes"]
    home = next(v for v in volumes if v["name"] == "home")
    assert "emptyDir" in home, f"HOME volume is not an emptyDir: {home!r}"
    assert home["emptyDir"].get("sizeLimit"), (
        "the sandbox HOME emptyDir was submitted without a sizeLimit — the "
        "kubelet has no per-volume disk bound to enforce, so one busy sandbox "
        f"can exhaust the node's root filesystem; volume: {home!r}"
    )


def test_ephemeral_storage_resources_accepted_and_forwarded(tmp_path: Path) -> None:
    """Configured ephemeral-storage must survive config parse into the pod.

    Regression guard: the server-side allowlist used to reject the key at
    startup, and even when it passed, the launcher forwarded only cpu/memory
    — so sandboxes carried request 0 for ephemeral-storage and the scheduler
    could not spread them by disk.
    """
    port = _find_free_port()
    config_path = _write_server_config(
        tmp_path,
        port,
        {
            "requests": {"cpu": "500m", "memory": "1Gi", "ephemeral-storage": "2Gi"},
            "limits": {"cpu": "2", "memory": "4Gi", "ephemeral-storage": "8Gi"},
        },
    )
    capture_path = tmp_path / "submitted.json"
    proc, log_path = _spawn_server(tmp_path, config_path, port, capture_path)
    try:
        base_url = f"http://127.0.0.1:{port}"
        # Would fail here if startup regressed to rejecting 'ephemeral-storage'.
        _wait_for_health(proc, base_url, log_path)
        _create_managed_session(base_url)
        manifest = _await_submitted_job_manifest(capture_path, log_path)
    finally:
        proc.kill()
        proc.wait(timeout=30)

    pod = _pod_spec(manifest)
    containers = pod.get("initContainers", []) + pod["containers"]
    assert containers
    for container in containers:
        resources = container.get("resources") or {}
        for tier, expected in (("requests", "2Gi"), ("limits", "8Gi")):
            actual = (resources.get(tier) or {}).get("ephemeral-storage")
            assert actual == expected, (
                f"container {container['name']!r} {tier} dropped the configured "
                f"ephemeral-storage (expected {expected!r}, got {actual!r}); "
                f"resources: {resources!r}"
            )
