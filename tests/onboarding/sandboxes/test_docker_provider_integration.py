"""Opt-in integration test for the Docker sandbox provider.

Exercises the real managed subset (provision → run → terminate) against a
live Docker daemon. Uses a small image that ships bash (default
python:3.12-slim) so it does not require the private Omnigent host image.

Runs in the normal suite but auto-skips without a Docker daemon. Select
explicitly with: ``pytest -m docker_sandbox tests/onboarding/sandboxes``.
"""

from __future__ import annotations

import os

import pytest

from omnigent.onboarding.sandboxes.docker import DockerSandboxLauncher
from tests.onboarding.sandboxes._docker_it import docker_available, image_available

TEST_IMAGE = os.environ.get("OMNIGENT_DOCKER_TEST_IMAGE", "python:3.12-slim")

pytestmark = [
    pytest.mark.docker_sandbox,
    pytest.mark.skipif(not docker_available(), reason="Docker daemon is not available"),
    pytest.mark.skipif(
        not image_available(TEST_IMAGE), reason=f"test image unavailable: {TEST_IMAGE}"
    ),
]


def test_real_docker_provision_run_terminate() -> None:
    import docker

    launcher = DockerSandboxLauncher(
        image=TEST_IMAGE,
        # Default bridge so we don't depend on the compose-defined network.
        network=os.environ.get("OMNIGENT_DOCKER_TEST_NETWORK", "bridge"),
        resources={"mem_limit": "512m", "nano_cpus": 500_000_000, "pids_limit": 128},
        # Generic image; the goal is provision/run/terminate, not bwrap posture.
        security={},
    )
    launcher.prepare()
    sandbox_id = launcher.provision("managed-integration")
    try:
        ok = launcher.run(sandbox_id, "printf DOCKER_PROVIDER_OK")
        assert ok.returncode == 0
        assert ok.stdout == "DOCKER_PROVIDER_OK"

        # Non-zero exit with check=False returns the result; stderr is demuxed.
        err = launcher.run(sandbox_id, "echo boom 1>&2; exit 7", check=False)
        assert err.returncode == 7
        assert err.stderr.strip() == "boom"

        # Labels are stamped for the reaper.
        labels = docker.from_env().containers.get(sandbox_id).labels
        assert labels["omnigent.managed"] == "1"
        assert labels["omnigent.provider"] == "docker"
    finally:
        launcher.terminate(sandbox_id)

    # Terminate is idempotent and the container is really gone.
    launcher.terminate(sandbox_id)
    with pytest.raises(docker.errors.NotFound):
        docker.from_env().containers.get(sandbox_id)
