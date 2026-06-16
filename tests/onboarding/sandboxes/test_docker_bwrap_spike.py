"""A2 spike: does Omnigent's inner sandbox work inside the host container?

Opt-in (needs a Docker daemon and the Omnigent host image). Validates the
execution boundary the Docker provider relies on, before the provider's
security hardening is finalized:

- ``os_env.sandbox.type: none`` resolves to an inactive policy (the
  container is the isolation boundary), and
- the default ``linux_bwrap`` backend can ACTIVATE its namespaces inside
  the stock unprivileged host container — exercising the real launcher
  path (``create_exec_launcher`` → ``run_launcher`` → bwrap), not just
  ``which bwrap``.

If the bwrap test fails inside a plain unprivileged container, the host's
kernel likely disables unprivileged user namespaces. Then native
harnesses (claude-native/codex-native/pi) need a userns-enabled host or
extra privileges; non-native agents must use ``os_env.sandbox.type: none``.
Record the outcome in the spec (A2/A5) and deploy/docker/README.md.

Auto-skips when the host image is unavailable. Select explicitly with:
``pytest -m docker_sandbox tests/onboarding/sandboxes``.
"""

from __future__ import annotations

import os
import subprocess
import uuid

import pytest

from tests.onboarding.sandboxes._docker_it import docker_available, image_available

HOST_IMAGE = os.environ.get(
    "OMNIGENT_DOCKER_HOST_IMAGE", "ghcr.io/omnigent-ai/omnigent-host:latest"
)

pytestmark = [
    pytest.mark.docker_sandbox,
    pytest.mark.skipif(not docker_available(), reason="Docker daemon is not available"),
    pytest.mark.skipif(
        not image_available(HOST_IMAGE),
        reason=f"Omnigent host image unavailable: {HOST_IMAGE}",
    ),
]


def _userns_denied(stderr: str) -> bool:
    """True when bwrap failed because the host kernel disallows user namespaces."""
    markers = (
        "Creating new namespace failed",
        "kernel does not allow",
        "No permissions to create new namespace",
    )
    return any(m in stderr for m in markers)


def _run_host_image(command: str) -> subprocess.CompletedProcess[str]:
    name = f"omnigent-bwrap-spike-{uuid.uuid4().hex[:10]}"
    return subprocess.run(
        ["docker", "run", "--rm", "--name", name, HOST_IMAGE, "bash", "-lc", command],
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )


def test_host_image_resolves_os_env_none_to_inactive_policy() -> None:
    result = _run_host_image(
        "python - <<'PY'\n"
        "from pathlib import Path\n"
        "from omnigent.inner.datamodel import OSEnvSpec, OSEnvSandboxSpec\n"
        "from omnigent.inner.sandbox import resolve_sandbox\n"
        "spec = OSEnvSpec(type='caller_process', cwd='/tmp', "
        "sandbox=OSEnvSandboxSpec(type='none'))\n"
        "policy = resolve_sandbox(spec, Path('/tmp'))\n"
        "print(policy.backend_type, policy.active)\n"
        "PY"
    )
    assert result.returncode == 0, result.stderr
    assert "none False" in result.stdout


def test_host_image_can_activate_linux_bwrap_full_path() -> None:
    result = _run_host_image(
        "python - <<'PY'\n"
        "import subprocess, sys\n"
        "from pathlib import Path\n"
        "from omnigent.inner.datamodel import OSEnvSpec, OSEnvSandboxSpec\n"
        "from omnigent.inner.sandbox import create_exec_launcher, resolve_sandbox\n"
        "cwd = Path('/tmp').resolve()\n"
        "spec = OSEnvSpec(type='caller_process', cwd=str(cwd), "
        "sandbox=OSEnvSandboxSpec(type='linux_bwrap', allow_network=True))\n"
        "policy = resolve_sandbox(spec, cwd)\n"
        "launcher = create_exec_launcher(sys.executable, policy)\n"
        "proc = subprocess.run([launcher, '-c', 'print(\"BWRAP_FULL_PATH_OK\")'], "
        "text=True, capture_output=True, check=False)\n"
        "print(proc.stdout, end='')\n"
        "print(proc.stderr, end='', file=sys.stderr)\n"
        "raise SystemExit(proc.returncode)\n"
        "PY"
    )
    if result.returncode != 0 and _userns_denied(result.stderr):
        pytest.skip(
            "host kernel disallows unprivileged user namespaces "
            "(bwrap: 'Creating new namespace failed: Operation not permitted'). "
            "Native harnesses (claude-native/codex-native/pi) need a "
            "userns-enabled host; non-native agents can run with "
            "os_env.sandbox.type: none. Enable unprivileged userns "
            "(e.g. sysctl kernel.unprivileged_userns_clone=1) to run this test."
        )
    assert result.returncode == 0, result.stderr
    assert "BWRAP_FULL_PATH_OK" in result.stdout
