"""
End-to-end guard: a managed-sandbox clone must keep the
shared ``$GIT_TOKEN`` helper on a server with **no GitHub connection
configured**.

User journey (operator + user):

1. An operator runs a server with no GitHub App integration (``/v1/info``
   reports ``enabled_connections: []``) and ``sandbox.provider: kubernetes``
   with ``sandbox.kubernetes.secret_name`` supplying ``GIT_TOKEN`` (the shared
   fleet clone credential the host image's system-scope credential helper
   reads).
2. A user creates a managed session whose workspace is a **private** GitHub
   repository URL (``POST /v1/sessions`` with ``host_type: "managed"`` and
   ``workspace: "https://github.com/..."``).
3. The launcher submits the sandbox Job; its ``workspace-prep`` init container
   wires clone credentials (``configure_clone_credentials`` — which probes the
   server's credential endpoint and gets a **404 "unknown credential
   provider"**, since no GitHub provider is configured) and then runs
   ``git clone``.
4. Expected: the clone authenticates with the shared ``$GIT_TOKEN`` (the 404
   means "this server vends no GitHub credential for anyone", so the ambient
   helper chain must be kept). Bug: the inconclusive-probe path installs the
   per-user broker anyway, whose chain reset **disarms** the image's shared
   ``$GIT_TOKEN`` helper; the broker itself vends nothing (the endpoint 404s),
   so the clone dies ``fatal: could not read Username for
   'https://github.com'`` and the sandbox launch fails with it.

The apiserver is unreachable from the test environment, so the stub
``kubernetes`` SDK (:mod:`tests.e2e._k8s_stub_sdk`) stands in for the cluster
and records the launch-token Secret + Job manifests the real launcher submits.
The test then acts as the kubelet for the captured ``workspace-prep`` init
container: it executes the manifest's command verbatim with the manifest's env
(the launch token resolved from the captured Secret, ``GIT_TOKEN`` standing in
for the operator's projected harness Secret, and the host image's system-scope
credential helper from ``deploy/docker/Dockerfile``). Real github.com is not
reachable either, so a loopback CONNECT proxy terminates TLS for
``github.com`` itself and serves the private repository over smart HTTP behind
Basic auth (:mod:`tests.e2e._fake_github_https`) — the clone URL, and
therefore git's credential-helper context, stays ``https://github.com``.
Everything else is real: the server process, the managed-session HTTP journey,
the launcher, the credential endpoint's 404, the init container's exact
command, and git's credential resolution.
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

from omnigent.host.identity import (
    HOST_ID_ENV_VAR,
    HOST_TOKEN_ENV_VAR,
    MANAGED_HOST_TOKEN_HEADER,
)
from omnigent.onboarding.sandboxes.kubernetes import _HOME_DIR as _POD_HOME_DIR
from tests.e2e._fake_github_https import FakeGitHub, make_bare_repo
from tests.e2e._k8s_stub_sdk import CAPTURE_ENV_VAR as _CAPTURE_ENV_VAR
from tests.e2e._k8s_stub_sdk import STUB_FILES as _STUB_FILES

_REPO_ROOT = Path(__file__).resolve().parents[2]

_HEALTH_TIMEOUT_S = 180.0
_CAPTURE_TIMEOUT_S = 120.0
_POLL_INTERVAL_S = 0.5
_PREP_TIMEOUT_S = 180.0

# The private repository of the reported journey, served by the loopback
# github.com stand-in. The host must stay github.com: the broker disarm the
# bug turns on is keyed to ``credential.https://github.com.helper``.
_ORG_REPO = "acme/private-widget"
_CLONE_URL = f"https://github.com/{_ORG_REPO}.git"

# The shared fleet clone token the operator's harness Secret supplies as
# ``GIT_TOKEN`` (report step 2: ``sandbox.kubernetes.secret_name``).
_SHARED_GIT_TOKEN = "shared-fleet-git-token"

# The exact system-scope credential helper the managed host image installs
# (deploy/docker/Dockerfile): answers ``git credential get`` for any host from
# $GIT_TOKEN / $GIT_USERNAME, emitting nothing when GIT_TOKEN is unset.
_IMAGE_CREDENTIAL_HELPER = (
    '!f() { [ "$1" = get ] || return 0; [ -n "$GIT_TOKEN" ] || return 0; '
    'printf "username=%s\\npassword=%s\\n" "${GIT_USERNAME:-x-access-token}" "$GIT_TOKEN"; }; f'
)

# Server boot (<=180s) + manifest capture (<=120s) can exceed the repo-default
# pytest-timeout on a loaded box.
pytestmark = pytest.mark.timeout(600)


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


def _write_server_config(tmp_path: Path, port: int) -> Path:
    """Write a server config: kubernetes sandboxes, NO GitHub connection.

    ``pod_ready_timeout_s`` is high on purpose: in a real cluster the
    workspace-prep init container runs while the server is still waiting for
    the pod to become ready, and this test executes the captured init
    container inside exactly that window (launch token armed, host row live).
    """
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
                        # The operator Secret that supplies GIT_TOKEN to the
                        # init container via envFrom (report step 2).
                        "secret_name": "omnigent-harness-secrets",
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


def _create_managed_repo_session(base_url: str) -> None:
    """Drive the user journey: a managed session cloning a private repo."""
    info = httpx.get(f"{base_url}/v1/info", timeout=10.0).json()
    assert info.get("managed_sandboxes_enabled") is True
    # Report step 1: this deployment has no GitHub connection provider.
    assert info.get("enabled_connections") == [], (
        f"harness setup: expected no configured connection providers, got "
        f"{info.get('enabled_connections')!r}"
    )
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
    """Create the init container's HOME, with the image-profile PATH shim.

    The init container runs ``bash -lc``; the managed host image's login
    profile puts its venv on PATH. ``~/.profile`` reproduces that here (and
    repairs any ``/etc/profile`` PATH reset on the box running the test) so
    the script's ``python3`` resolves to this interpreter.
    """
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
    """Build the env the kubelet would give the workspace-prep container.

    Manifest env is honored verbatim (the launch token resolved from the
    captured Secret, ``HOME`` relocated with the pod filesystem); around it sit
    the pieces the image/operator provide in a real pod: the projected harness
    Secret (``GIT_TOKEN``), the image's system-scope git helper, no TTY
    (``GIT_TERMINAL_PROMPT=0``), and the loopback github.com transport.
    """
    env: dict[str, str] = {}
    for entry in init_container["env"]:
        if "value" in entry:
            env[entry["name"]] = str(entry["value"]).replace(_POD_HOME_DIR, str(pod_home))
        else:
            key = entry["valueFrom"]["secretKeyRef"]["key"]
            env[entry["name"]] = secret_data[key]
    # The operator's harness Secret rides envFrom in the manifest; the
    # stand-in kubelet projects its GIT_TOKEN key here (report step 2).
    assert init_container.get("envFrom"), (
        "harness setup: the init container lost its envFrom harness-Secret "
        "projection — GIT_TOKEN would never reach the clone"
    )
    env["GIT_TOKEN"] = _SHARED_GIT_TOKEN
    env.update(
        {
            "PATH": os.pathsep.join([str(Path(sys.executable).parent), os.environ["PATH"]]),
            "PYTHONPATH": _pythonpath(),
            "GIT_CONFIG_SYSTEM": str(system_cfg),
            # A pod has no TTY: a credential-less HTTPS clone dies with
            # "could not read Username" instead of prompting.
            "GIT_TERMINAL_PROMPT": "0",
            # The github.com stand-in's self-signed certificate.
            "GIT_SSL_NO_VERIFY": "1",
            "https_proxy": proxy_url,
            "HTTPS_PROXY": proxy_url,
            "http_proxy": proxy_url,
            "HTTP_PROXY": proxy_url,
            # The wire step's probe of the (loopback) server must not ride the
            # github.com proxy.
            "no_proxy": "127.0.0.1,localhost",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    return env


def test_shared_git_token_clone_survives_no_provider_probe(tmp_path: Path) -> None:
    """A 404 from the credential endpoint must not disarm the shared $GIT_TOKEN helper.

    Fails on the bug with the init container's own journey: the workspace-prep
    command exits non-zero, its ``git clone`` dying ``fatal: could not read
    Username for 'https://github.com'`` even though ``GIT_TOKEN`` is set and
    armed, because the broker probe treated the definitive 404 ("no GitHub
    provider on this server") as inconclusive and reset the github.com helper
    chain. Passes when the clone completes with the shared token.
    """
    port = _find_free_port()
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

        init_container = job_manifest["spec"]["template"]["spec"]["initContainers"][0]
        assert init_container["name"] == "workspace-prep"
        command = init_container["command"]
        script = command[-1]
        secret_data = secret_manifest["stringData"]
        host_token = secret_data[HOST_TOKEN_ENV_VAR]

        # The reported trigger: with the launch token armed, this server's
        # credential endpoint vends nothing for github (today a 404 "unknown
        # credential provider"). Guard that a connected credential never
        # appears — if one did, the journey under test wouldn't exist.
        assert "configure_clone_credentials" in script, (
            f"no credential wire step in workspace-prep script:\n{script}"
        )
        host_container = job_manifest["spec"]["template"]["spec"]["containers"][0]
        host_id = next(e["value"] for e in host_container["env"] if e["name"] == HOST_ID_ENV_VAR)
        probe = httpx.get(
            f"{base_url}/v1/hosts/{host_id}/credentials/github",
            headers={MANAGED_HOST_TOKEN_HEADER: host_token},
            timeout=10.0,
        )
        assert probe.status_code != 401, (
            "harness setup: the captured launch token does not resolve"
        )
        # Pin the exact trigger under test: the no-provider 404. A 200
        # ``connected: false`` would exercise the (already-working)
        # confirmed-unlinked path instead, and the pre-fix code would pass.
        assert probe.status_code == 404, (
            "harness setup: expected the no-provider 404 from the credential "
            f"endpoint, got HTTP {probe.status_code}: {probe.text[:200]}"
        )

        # Stand-in kubelet + github.com: image git identity, projected
        # GIT_TOKEN, loopback github.com serving the private repository.
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

            # Control: with the image's ambient chain intact (no broker
            # wiring), the shared-token clone of this private repo works. If
            # THIS fails the harness lane is broken, not the product.
            control_home = tmp_path / "control-home"
            control_home.mkdir()
            control = subprocess.run(
                ["git", "clone", "--", _CLONE_URL, str(tmp_path / "control-clone")],
                env={**env, "HOME": str(control_home)},
                capture_output=True,
                text=True,
                timeout=_PREP_TIMEOUT_S,
            )
            assert control.returncode == 0, (
                f"harness setup: shared-token clone with the ambient chain failed:\n"
                f"{control.stderr[-2000:]}"
            )

            # The journey: execute the captured workspace-prep init container
            # verbatim (only the pod filesystem root is relocated).
            prep = subprocess.run(
                [*command[:-1], script.replace(_POD_HOME_DIR, str(pod_home))],
                env=env,
                capture_output=True,
                text=True,
                timeout=_PREP_TIMEOUT_S,
            )

        # The wire step is best-effort (`|| true`): a crashed probe would skip
        # the broker path entirely and mask the bug with a false pass.
        assert "Traceback" not in prep.stdout + prep.stderr, (
            f"harness setup: the credential wire step crashed instead of running:\n"
            f"{(prep.stdout + prep.stderr)[-2000:]}"
        )

        helpers_after = subprocess.run(
            ["git", "config", "--global", "--get-all", "credential.https://github.com.helper"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30.0,
        ).stdout
        clone_landed = (pod_home / "workspace" / "private-widget" / "README.md").exists()
        assert prep.returncode == 0 and clone_landed, (
            "workspace-prep failed to clone the private repository with the shared "
            "$GIT_TOKEN on a server with no GitHub connection configured: the broker "
            "probe's 404 (no provider) must keep the image's ambient credential chain, "
            "but the clone could not authenticate.\n"
            f"credential endpoint probe: HTTP {probe.status_code} {probe.text[:120]!r}\n"
            f"workspace-prep exit code: {prep.returncode}\n"
            f"workspace-prep stdout:\n{prep.stdout[-1000:]}\n"
            f"workspace-prep stderr:\n{prep.stderr[-2000:]}\n"
            f"github.com helper chain after workspace-prep (--global):\n{helpers_after}"
        )
    finally:
        proc.kill()
        proc.wait(timeout=30)
