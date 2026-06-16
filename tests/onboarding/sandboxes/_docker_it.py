"""Skip guards shared by the opt-in Docker provider integration/spike tests.

Not a test module (no ``test_`` prefix), so pytest does not collect it.
These tests live in the normal suite but auto-skip when there is no Docker
daemon (or, for the spike, no Omnigent host image). Select/deselect them
with ``-m docker_sandbox`` / ``-m 'not docker_sandbox'``.
"""

from __future__ import annotations

import shutil
import subprocess


def docker_available() -> bool:
    """True when the docker CLI exists and a daemon answers ``docker info``."""
    if shutil.which("docker") is None:
        return False
    return (
        subprocess.run(
            ["docker", "info"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def image_available(image: str) -> bool:
    """True when *image* can be inspected locally or pulled."""
    if not docker_available():
        return False
    present = subprocess.run(
        ["docker", "image", "inspect", image],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if present.returncode == 0:
        return True
    return (
        subprocess.run(
            ["docker", "pull", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            timeout=300,
        ).returncode
        == 0
    )
