"""Regression tests for the OpenShell host image and Kubernetes overlay."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[2]
_DOCKERFILE = _ROOT / "deploy/docker/Dockerfile"
_OVERLAY = _ROOT / "deploy/kubernetes/overlays/openshell"
_GATEWAY_SECRET = "openshell-gateway-config"


def _stage_instructions(stage: str) -> list[str]:
    text = _DOCKERFILE.read_text().replace("\\\n", " ")
    for body in re.split(r"^FROM\s", text, flags=re.MULTILINE)[1:]:
        lines = body.splitlines()
        if re.search(rf"\bAS\s+{stage}\s*$", lines[0]):
            stripped = (line.strip() for line in lines[1:])
            return [line for line in stripped if line and not line.startswith("#")]
    raise AssertionError(f"stage {stage!r} not found in {_DOCKERFILE}")


def _server_pod() -> dict:
    patch = yaml.safe_load((_OVERLAY / "deployment-patch.yaml").read_text())
    return patch["spec"]["template"]["spec"]


def _openshell_config() -> dict:
    server_config = yaml.safe_load((_OVERLAY / "server-config.yaml").read_text())["data"]
    return yaml.safe_load(server_config["config.yaml"])["sandbox"]["openshell"]


def test_host_image_venv_is_writable_by_openshell_sandbox_user() -> None:
    """OpenShell execs run as ``sandbox`` and pip-install Omnigent wheels into /opt/venv."""
    instructions = _stage_instructions("host")
    venv_writes = [
        index
        for index, line in enumerate(instructions)
        if re.match(r"COPY\b.*\s/opt/venv$", line) or re.match(r"RUN\b.*\bpip install\b", line)
    ]
    ownership_grants = [
        index
        for index, line in enumerate(instructions)
        if re.match(r"COPY\b.*--chown=(sandbox|1000660000)\b.*\s/opt/venv$", line)
        or re.match(r"RUN\b.*\bchown\b.*\b(sandbox|1000660000)\b.*\s/opt/venv\b", line)
    ]

    assert venv_writes
    assert ownership_grants and ownership_grants[-1] >= venv_writes[-1]


def _server_sdk_config_home(container: dict) -> str:
    runtime = _stage_instructions("runtime")
    users = [line.split()[1] for line in runtime if line.startswith("USER ")]
    root_user = not users or users[-1] in ("root", "0")
    environment = {"HOME": "/root" if root_user else f"/home/{users[-1]}"}
    for line in runtime:
        if line.startswith("ENV "):
            environment.update(re.findall(r"\b(HOME|XDG_CONFIG_HOME)=\"?([^\s\"]+)", line))
    configmap = yaml.safe_load((_OVERLAY / "configmap-patch.yaml").read_text())["data"]
    environment.update(
        {key: configmap[key] for key in ("HOME", "XDG_CONFIG_HOME") if key in configmap}
    )
    environment.update({item["name"]: item.get("value") for item in container.get("env", [])})
    return environment.get("XDG_CONFIG_HOME") or f"{environment['HOME']}/.config"


def test_openshell_overlay_mounts_gateway_config_where_server_sdk_reads_it() -> None:
    """The server's SDK reads ``${XDG_CONFIG_HOME:-$HOME/.config}/openshell``."""
    pod = _server_pod()
    container = pod["containers"][0]
    gateway_volumes = {
        volume["name"]
        for volume in pod["volumes"]
        if volume.get("secret", {}).get("secretName") == _GATEWAY_SECRET
    }
    mount_paths = [
        mount["mountPath"]
        for mount in container["volumeMounts"]
        if mount["name"] in gateway_volumes
    ]

    assert mount_paths == [f"{_server_sdk_config_home(container)}/openshell"]


def test_openshell_overlay_gateway_secret_projects_gateway_metadata() -> None:
    """Secret keys cannot contain ``/``, so items place them under the configured gateway."""
    # Without a cluster name the SDK falls back to an active_gateway file.
    cluster = _openshell_config().get("cluster")
    assert cluster, "sandbox.openshell.cluster must name the mounted gateway"
    (secret,) = [
        volume["secret"]
        for volume in _server_pod()["volumes"]
        if volume.get("secret", {}).get("secretName") == _GATEWAY_SECRET
    ]
    projected = {item["key"]: item["path"] for item in secret.get("items", [])}

    assert projected == {
        "metadata.json": f"gateways/{cluster}/metadata.json",
        **{key: f"gateways/{cluster}/mtls/{key}" for key in ("ca.crt", "tls.crt", "tls.key")},
    }
    # Plaintext gateways have no mTLS keys; optional lets the pod start without them.
    assert secret["optional"] is True


def test_openshell_overlay_allows_server_egress_to_in_cluster_gateway() -> None:
    """Egress is checked against the gateway pod's port after Service translation."""
    base = yaml.safe_load((_ROOT / "deploy/kubernetes/base/deployment.yaml").read_text())
    policy = yaml.safe_load((_OVERLAY / "gateway-networkpolicy.yaml").read_text())["spec"]
    gateway_namespace = {"kubernetes.io/metadata.name": "openshell"}

    assert policy["podSelector"]["matchLabels"].items() <= (
        base["spec"]["template"]["metadata"]["labels"].items()
    )
    assert any(
        {"protocol": "TCP", "port": 8080} in rule.get("ports", [])
        and {"namespaceSelector": {"matchLabels": gateway_namespace}} in rule.get("to", [])
        for rule in policy["egress"]
    )


def test_openshell_overlay_forwards_runner_proxy_passthrough_into_sandboxes() -> None:
    """Runners only inherit the sandbox proxy when the host receives the passthrough list."""
    server_env = yaml.safe_load((_OVERLAY / "configmap-patch.yaml").read_text())["data"]
    passthrough = set(server_env["OMNIGENT_RUNNER_ENV_PASSTHROUGH"].split(","))

    assert "OMNIGENT_RUNNER_ENV_PASSTHROUGH" in _openshell_config()["env"]
    assert {"HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"} <= passthrough
