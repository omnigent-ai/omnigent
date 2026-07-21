#!/usr/bin/env python3
"""
End-to-end test: `sandbox.host_config` injection on the Kubernetes provider.

Runs against an EXISTING omnigent server already configured with
``sandbox.provider: kubernetes`` and a ``sandbox.host_config:`` block (see
``deploy/kubernetes/overlays/sandbox-runners/`` — apply the overlay, put a
``host_config:`` block in ``sandbox-config.yaml``, and make the server URL
reachable from where this script runs, e.g. via ``kubectl port-forward``).

The script creates a managed session, waits for the runner Pod's host to
register, then ``kubectl exec``'s into the Pod and asserts the injected
config landed at ``/home/omnigent/.omnigent/config.yaml`` before the host
came up. It needs ``kubectl`` on PATH with access to the runner namespace.

    python tests/e2e/integrations/deploy/kubernetes/e2e_managed_host_config.py \
        --server http://localhost:8080
"""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
import time

import httpx

# Constants pinned by the kubernetes launcher (see
# omnigent/onboarding/sandboxes/kubernetes.py): the Pod's fixed HOME, the
# main container and host-id env names, and the labels stamped on every runner Pod.
POD_HOME = "/home/omnigent"
HOST_CONTAINER = "host"
HOST_ID_ENV_VAR = "OMNIGENT_HOST_ID"
POD_SELECTOR = "app.kubernetes.io/managed-by=omnigent,omnigent.ai/role=sandbox-host"
DEFAULT_EXPECTED_CONFIG = (
    "litellm:",
    "base_url: http://litellm.litellm.svc.cluster.local/v1",
)


def log(msg: str) -> None:
    print(msg, flush=True)


def check_server(base: str) -> None:
    log(f"[1/5] checking {base}/v1/info")
    info = httpx.get(f"{base}/v1/info", timeout=10.0).json()
    if not info.get("managed_sandboxes_enabled"):
        raise SystemExit("server does not advertise managed sandboxes — is sandbox: configured?")
    if info.get("sandbox_provider") != "kubernetes":
        raise SystemExit(
            f"server's sandbox provider is {info.get('sandbox_provider')!r}, not 'kubernetes'"
        )
    log("      ✓ managed sandboxes enabled (kubernetes)")


def pick_agent(base: str, agent_id: str | None) -> str:
    resp = httpx.get(f"{base}/v1/agents", timeout=10.0)
    resp.raise_for_status()
    agents = resp.json()["data"]
    if not agents:
        raise SystemExit("no agents registered on the server to bind a session to")
    if agent_id:
        if not any(a.get("id") == agent_id for a in agents):
            raise SystemExit(f"agent_id {agent_id!r} not found on the server")
        return agent_id
    chosen = agents[0]
    log(f"      agent_id={chosen['id']} ({chosen.get('name')})")
    return chosen["id"]


def create_managed_session(base: str, agent_id: str) -> str:
    log("[2/5] creating managed session")
    r = httpx.post(
        f"{base}/v1/sessions",
        json={"agent_id": agent_id, "host_type": "managed"},
        timeout=180.0,
    )
    if r.status_code >= 300:
        raise SystemExit(f"create session failed: HTTP {r.status_code}: {r.text[:600]}")
    conv_id = r.json()["id"]
    log(f"      session={conv_id}")
    return conv_id


def wait_host_online(base: str, conv_id: str, timeout_s: float) -> str:
    log("[3/5] waiting for the runner Pod's host to register")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        d = httpx.get(f"{base}/v1/sessions/{conv_id}", timeout=10.0).json()
        if d.get("host_id"):
            log(f"      ✓ host online: host_id={d['host_id']}")
            return d["host_id"]
        status = d.get("sandbox_status") or {}
        if status.get("stage") == "failed":
            raise SystemExit(f"managed launch failed: {status.get('error')}")
        time.sleep(5.0)
    raise SystemExit(f"host did not come online within {timeout_s:.0f}s")


def runner_pod_for_host(kubectl: str, namespace: str, host_id: str) -> str:
    out = subprocess.run(
        [
            *shlex.split(kubectl),
            "get",
            "pods",
            "-n",
            namespace,
            "-l",
            POD_SELECTOR,
            "-o",
            "json",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    pods = json.loads(out).get("items", [])
    matches = []
    for pod in pods:
        containers = pod.get("spec", {}).get("containers", [])
        host = next((item for item in containers if item.get("name") == HOST_CONTAINER), None)
        env = host.get("env", []) if host else []
        if any(
            item.get("name") == HOST_ID_ENV_VAR and item.get("value") == host_id for item in env
        ):
            matches.append(pod["metadata"]["name"])
    if not matches:
        raise SystemExit(f"no runner Pod matching host_id {host_id!r} in namespace {namespace!r}")
    if len(matches) > 1:
        raise SystemExit(f"multiple runner Pods match host_id {host_id!r}: {', '.join(matches)}")
    return f"pod/{matches[0]}"


def assert_injected_config(
    kubectl: str, namespace: str, pod: str, expected: list[str] | tuple[str, ...]
) -> str:
    log(f"[4/5] reading {POD_HOME}/.omnigent/config.yaml from {pod}")
    proc = subprocess.run(
        [
            *shlex.split(kubectl),
            "exec",
            "-n",
            namespace,
            pod,
            "-c",
            HOST_CONTAINER,
            "--",
            "cat",
            f"{POD_HOME}/.omnigent/config.yaml",
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"config.yaml missing in the runner Pod — host_config was not injected?\n"
            f"{proc.stderr.strip()}"
        )
    content = proc.stdout
    log("      --- config.yaml ---")
    log(content.rstrip())
    missing = [fragment for fragment in expected if fragment not in content]
    if missing:
        raise SystemExit(f"config.yaml does not contain expected fragment(s): {missing!r}")
    log(f"      ✓ contains expected fragments: {expected!r}")
    return content


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, help="Omnigent server base URL")
    parser.add_argument("--agent-id", default=None, help="Agent to bind (default: first)")
    parser.add_argument("--namespace", default="omnigent-sandboxes", help="Runner-Pod namespace")
    parser.add_argument(
        "--expect",
        action="append",
        default=None,
        help=(
            "Substring the injected config.yaml must contain; repeat for multiple fragments "
            "(default: the documented litellm provider and base_url)"
        ),
    )
    parser.add_argument(
        "--kubectl",
        default="kubectl",
        help="kubectl command, split shell-style (e.g. 'kubectl --context my-cluster')",
    )
    parser.add_argument("--timeout", type=float, default=300.0, help="Host-online wait (s)")
    parser.add_argument("--keep", action="store_true", help="Skip session cleanup")
    args = parser.parse_args()
    base = args.server.rstrip("/")

    check_server(base)
    agent_id = pick_agent(base, args.agent_id)
    conv_id = create_managed_session(base, agent_id)
    try:
        host_id = wait_host_online(base, conv_id, args.timeout)
        pod = runner_pod_for_host(args.kubectl, args.namespace, host_id)
        assert_injected_config(
            args.kubectl,
            args.namespace,
            pod,
            args.expect or DEFAULT_EXPECTED_CONFIG,
        )
    finally:
        if args.keep:
            log(f"[5/5] --keep: leaving session {conv_id} (and its Pod) running")
        else:
            log(f"[5/5] deleting session {conv_id} (terminates the runner Pod)")
            try:
                httpx.delete(f"{base}/v1/sessions/{conv_id}", timeout=60.0)
            except httpx.HTTPError as exc:
                log(f"      cleanup failed (Pod may linger): {exc}")
    log("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
