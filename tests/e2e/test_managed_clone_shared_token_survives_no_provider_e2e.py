"""Clone a private repo with the image's shared token when no provider is configured.

A stub Kubernetes SDK captures the real server's Job and Secret. Run the captured
workspace-prep command against a local authenticated GitHub stand-in, preserving
the github.com URL that selects Git's scoped credential helpers. No LLM is used.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.host.identity import (
    HOST_ID_ENV_VAR,
    HOST_TOKEN_ENV_VAR,
    MANAGED_HOST_TOKEN_HEADER,
)
from omnigent.onboarding.sandboxes.kubernetes import _HOME_DIR as _POD_HOME_DIR
from tests._helpers.live_server import find_free_port
from tests.e2e._fake_github_https import FakeGitHub, make_bare_repo
from tests.e2e._k8s_stub_sdk import CAPTURE_ENV_VAR as _CAPTURE_ENV_VAR
from tests.e2e._k8s_stub_sdk import STUB_FILES as _STUB_FILES

_REPO_ROOT = Path(__file__).resolve().parents[2]

_HEALTH_TIMEOUT_S = 180.0
_CAPTURE_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 0.5
_PREP_TIMEOUT_S = 180.0

_ORG_REPO = "acme/private-widget"
_CLONE_URL = f"https://github.com/{_ORG_REPO}.git"
_SHARED_GIT_TOKEN = "shared-fleet-git-token"
_HARNESS_SECRET = "omnigent-harness-secrets"

# Match the system-scope helper in deploy/docker/Dockerfile.
_IMAGE_CREDENTIAL_HELPER = (
    '!f() { [ "$1" = get ] || return 0; [ -n "$GIT_TOKEN" ] || return 0; '
    'printf "username=%s\\npassword=%s\\n" "${GIT_USERNAME:-x-access-token}" "$GIT_TOKEN"; }; f'
)

# Server boot (<=180s) + manifest capture (<=120s) can exceed the repo-default
# pytest-timeout on a loaded box.
pytestmark = pytest.mark.timeout(600)


def _write_stub_sdk(tmp_path: Path) -> Path:
    """Materialize the stub ``kubernetes`` package; return its sys.path root."""
    root = tmp_path / "k8s_stub"
    for rel, source in _STUB_FILES.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(source)
    return root


def _write_server_config(tmp_path: Path, port: int) -> Path:
    """Keep the launch token alive while the test runs the captured init command."""
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
                        "pod_ready_timeout_s": 600,
                        "secret_name": _HARNESS_SECRET,
                    },
                }
            }
        )
    )
    (tmp_path / "kubeconfig").write_text("")
    return config_path


def _pythonpath() -> str:
    """The PYTHONPATH the server subprocess and the init container use."""
    return os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            os.environ.get("PYTHONPATH", ""),
        ]
    )


def _spawn_server(
    tmp_path: Path, config_path: Path, port: int, capture_path: Path
) -> tuple[subprocess.Popen[bytes], Path]:
    """Start a real ``omnigent server`` subprocess wired to the stub SDK."""
    stub_root = _write_stub_sdk(tmp_path)
    env = {
        **os.environ,
        "PYTHONPATH": os.pathsep.join([str(stub_root), _pythonpath()]),
        _CAPTURE_ENV_VAR: str(capture_path),
        "OPENAI_API_KEY": "unused-no-turn-runs",
        "OMNIGENT_BUILTIN_AGENT_DIRS": str(
            _REPO_ROOT / "tests" / "resources" / "agents" / "sdk-chat-builtin.yaml"
        ),
    }
    log_path = tmp_path / "server.log"
    # The child keeps its own descriptor; the parent's copy closes right away.
    with open(log_path, "w") as log_handle:
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
            pass  # expected while the server is still booting; retry until deadline
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(f"server did not become healthy:\n{log_path.read_text()[-2000:]}")


def _create_managed_repo_session(base_url: str) -> None:
    """Drive the user journey: a managed session cloning a private repo."""
    info = httpx.get(f"{base_url}/v1/info", timeout=10.0).json()
    assert info.get("managed_sandboxes_enabled") is True
    assert info["enabled_connections"] == []
    agents = httpx.get(f"{base_url}/v1/agents", timeout=10.0).json()["data"]
    assert agents, "no agents registered on the server to bind a session to"
    response = httpx.post(
        f"{base_url}/v1/sessions",
        json={
            "agent_id": agents[0]["id"],
            "host_type": "managed",
            "workspace": _CLONE_URL,
        },
        timeout=120.0,
    )
    assert response.status_code == 201, (
        f"managed session create failed: HTTP {response.status_code}: {response.text[:500]}"
    )


def _await_capture(capture_path: Path, call: str, log_path: Path) -> dict:
    """Return the first captured stub-SDK record for *call*."""
    deadline = time.monotonic() + _CAPTURE_TIMEOUT_S
    while time.monotonic() < deadline:
        if capture_path.exists():
            records = json.loads(capture_path.read_text())
            matches = [r for r in records if r["call"] == call]
            if matches:
                return matches[0]
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(
        f"the launcher never submitted a {call!r} for the managed session:\n"
        f"{log_path.read_text()[-3000:]}"
    )


def _write_image_git_identity(tmp_path: Path) -> Path:
    """Materialize the host image's system-scope git credential helper."""
    system_cfg = tmp_path / "image-system.gitconfig"
    subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(system_cfg),
            "credential.helper",
            _IMAGE_CREDENTIAL_HELPER,
        ],
        check=True,
        capture_output=True,
        timeout=30.0,
    )
    return system_cfg


def _prepare_pod_home(tmp_path: Path) -> Path:
    """Mirror the image's login profile so bash -lc uses the test interpreter."""
    pod_home = tmp_path / "pod-home"
    pod_home.mkdir()
    venv_bin = Path(sys.executable).parent
    (pod_home / ".profile").write_text(f'PATH="{venv_bin}:$PATH"\nexport PATH\n')
    return pod_home


def _init_container_env(
    init_container: dict,
    secret_data: dict[str, str],
    *,
    pod_home: Path,
    system_cfg: Path,
    proxy_url: str,
) -> dict[str, str]:
    """Resolve the manifest's env and add the image helper and harness Secret."""
    env: dict[str, str] = {}
    for entry in init_container["env"]:
        if "value" in entry:
            env[entry["name"]] = str(entry["value"]).replace(_POD_HOME_DIR, str(pod_home))
        else:
            key = entry["valueFrom"]["secretKeyRef"]["key"]
            env[entry["name"]] = secret_data[key]
    assert init_container["envFrom"] == [{"secretRef": {"name": _HARNESS_SECRET}}]
    env["GIT_TOKEN"] = _SHARED_GIT_TOKEN
    env.update(
        {
            "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ["PATH"]]),
            "PYTHONPATH": _pythonpath(),
            "GIT_CONFIG_SYSTEM": str(system_cfg),
            "GIT_TERMINAL_PROMPT": "0",
            # The github.com stand-in's self-signed certificate.
            "GIT_SSL_NO_VERIFY": "1",
            "https_proxy": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "HTTP_PROXY": proxy_url,
            # Keep broker requests direct; only GitHub uses the proxy.
            "no_proxy": "127.0.0.1,localhost",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    return env


def test_shared_git_token_clone_survives_no_provider_probe(tmp_path: Path) -> None:
    """The no-provider 404 must preserve shared credentials for workspace-prep."""
    port = find_free_port()
    config_path = _write_server_config(tmp_path, port)
    capture_path = tmp_path / "submitted.json"
    proc, log_path = _spawn_server(tmp_path, config_path, port, capture_path)
    try:
        base_url = f"http://127.0.0.1:{port}"
        _wait_for_health(proc, base_url, log_path)
        _create_managed_repo_session(base_url)
        secret_manifest = _await_capture(capture_path, "create_namespaced_secret", log_path)[
            "manifest"
        ]
        job_manifest = _await_capture(capture_path, "create_namespaced_job", log_path)["manifest"]

        pod = job_manifest["spec"]["template"]["spec"]
        init_container = pod["initContainers"][0]
        assert init_container["name"] == "workspace-prep"
        command = init_container["command"]
        script = command[-1]
        secret_data = secret_manifest["stringData"]
        assert "configure_clone_credentials" in script
        host_id = next(
            e["value"] for e in pod["containers"][0]["env"] if e["name"] == HOST_ID_ENV_VAR
        )
        probe = httpx.get(
            f"{base_url}/v1/hosts/{host_id}/credentials/github",
            headers={MANAGED_HOST_TOKEN_HEADER: secret_data[HOST_TOKEN_ENV_VAR]},
            timeout=10.0,
        )
        # A 200 connected:false would also pass before the fix; pin the trigger.
        assert probe.status_code == 404, probe.text
        assert probe.json() == {"detail": "unknown credential provider"}

        system_cfg = _write_image_git_identity(tmp_path)
        pod_home = _prepare_pod_home(tmp_path)
        github_root = tmp_path / "github"
        make_bare_repo(github_root, _ORG_REPO)
        with FakeGitHub(github_root, "x-access-token", _SHARED_GIT_TOKEN) as fake_github:
            env = _init_container_env(
                init_container,
                secret_data,
                pod_home=pod_home,
                system_cfg=system_cfg,
                proxy_url=fake_github.proxy_url,
            )

            # Prove the shared token works before running workspace-prep.
            control_home = tmp_path / "control-home"
            control_home.mkdir()
            control = subprocess.run(
                ["git", "clone", "--", _CLONE_URL, str(tmp_path / "control-clone")],
                env={**env, "HOME": str(control_home)},
                capture_output=True,
                text=True,
                timeout=_PREP_TIMEOUT_S,
            )
            assert control.returncode == 0, control.stderr

            # Run the captured command with only the pod filesystem relocated.
            prep = subprocess.run(
                [*command[:-1], script.replace(_POD_HOME_DIR, str(pod_home))],
                env=env,
                capture_output=True,
                text=True,
                timeout=_PREP_TIMEOUT_S,
            )

        output = prep.stdout + prep.stderr
        assert "Traceback" not in output, output
        assert prep.returncode == 0, output
        assert (pod_home / "workspace" / "private-widget" / "README.md").is_file()
    finally:
        proc.kill()
        proc.wait(timeout=30)
