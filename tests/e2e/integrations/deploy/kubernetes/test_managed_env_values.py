"""Exercise Pod-only env_values through a live managed-session launch.

Requires a disposable deployed server from this checkout, a real cluster, and
kubectl access to its runner Pods (including exec). Set
OMNIGENT_E2E_KUBERNETES_SERVER and/or OMNIGENT_E2E_AGENT_SANDBOX_SERVER to opt in.
See deploy/kubernetes/overlays/sandbox-runners/README.md for the server config
and CA Secret setup. No LLM call is needed: the feature runs at host startup.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess

import httpx
import pytest

from tests.e2e.integrations.deploy.kubernetes.e2e_managed_host_config import (
    HOST_CONTAINER,
    create_managed_session,
    pick_agent,
    runner_pod_for_host,
    wait_host_online,
)

_EXPECTED_ENV = {
    "SSL_CERT_FILE": "/mnt/omnigent-e2e-ca/ca.crt",
    "OMNIGENT_CONFIG_HOME": "/home/omnigent/e2e-config",
    "POD_ENV_VALUES_MARKER": "  pod-only literal  ",
    "POD_ENV_VALUES_EMPTY": "",
}


@pytest.mark.parametrize("provider", ["kubernetes", "agent_sandbox"])
@pytest.mark.timeout(600)
def test_managed_env_values_reaches_live_host(provider: str) -> None:
    server_var = f"OMNIGENT_E2E_{provider.upper()}_SERVER"
    base = os.environ.get(server_var, "").rstrip("/")
    if not base:
        pytest.skip(f"set {server_var} to a disposable server configured for env_values")
    kubectl = os.environ.get("OMNIGENT_E2E_KUBECTL", "kubectl")
    kubectl_args = shlex.split(kubectl)
    if not kubectl_args or shutil.which(kubectl_args[0]) is None:
        pytest.fail("kubectl must be on PATH when the live-cluster test is enabled")
    namespace = os.environ.get("OMNIGENT_E2E_KUBERNETES_NAMESPACE", "omnigent-sandboxes")

    info = httpx.get(f"{base}/v1/info", timeout=10.0)
    info.raise_for_status()
    assert info.json()["managed_sandboxes_enabled"]
    assert info.json()["sandbox_provider"] == provider
    agent_id = pick_agent(base, os.environ.get("OMNIGENT_E2E_AGENT_ID"))
    session_id = create_managed_session(base, agent_id)
    try:
        host_id = wait_host_online(base, session_id, timeout_s=300.0)
        host = httpx.get(f"{base}/v1/hosts/{host_id}", timeout=10.0)
        host.raise_for_status()
        assert host.json()["status"] == "online"
        pod = runner_pod_for_host(kubectl, namespace, host_id)
        manifest = json.loads(
            subprocess.run(
                [*kubectl_args, "get", "-n", namespace, pod, "-o", "json"],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            ).stdout
        )
        spec = manifest["spec"]
        container = next(c for c in spec["containers"] if c["name"] == HOST_CONTAINER)
        for name, value in _EXPECTED_ENV.items():
            assert {"name": name, "value": value} in container["env"]
        init = next(c for c in spec["initContainers"] if c["name"] == "workspace-prep")
        init_names = {entry["name"] for entry in init["env"]}
        assert init_names.isdisjoint(set(_EXPECTED_ENV) - {"OMNIGENT_CONFIG_HOME"})
        assert {
            "name": "OMNIGENT_CONFIG_HOME",
            "value": _EXPECTED_ENV["OMNIGENT_CONFIG_HOME"],
        } in init["env"]
        assert any(
            mount["mountPath"] == "/mnt/omnigent-e2e-ca" for mount in container["volumeMounts"]
        )
        assert all(
            mount["mountPath"] != "/mnt/omnigent-e2e-ca" for mount in init["volumeMounts"]
        )
        # Inspect the real container environment and load its mounted CA bundle,
        # not just the API object's intended environment.
        probe = """
import json, os, ssl, sys
from pathlib import Path

expected = json.loads(sys.argv[1])
actual = {name: os.environ.get(name) for name in expected}
assert actual == expected, actual
cert = Path(os.environ["SSL_CERT_FILE"])
assert cert.is_file(), cert
assert ssl.get_default_verify_paths().cafile == str(cert)
context = ssl.create_default_context()
assert context.cert_store_stats()["x509_ca"] > 0
print("env_values: real host environment and CA loading passed")
"""
        result = subprocess.run(
            [
                *kubectl_args,
                "exec",
                "-n",
                namespace,
                pod,
                "-c",
                HOST_CONTAINER,
                "--",
                "python3",
                "-c",
                probe,
                json.dumps(_EXPECTED_ENV),
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        assert "real host environment and CA loading passed" in result.stdout
        health = httpx.get(f"{base}/health", timeout=10.0)
        health.raise_for_status()
    finally:
        cleanup = httpx.delete(f"{base}/v1/sessions/{session_id}", timeout=60.0)
        cleanup.raise_for_status()
