"""Regression tests for managed host image CLI availability."""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "dockerfile",
    [
        _ROOT / "deploy/docker/Dockerfile",
        _ROOT / "deploy/docker/Dockerfile.ubi",
    ],
)
def test_host_images_install_kiro_cli_from_official_installer(dockerfile: Path) -> None:
    """Managed host images must preinstall the real Kiro CLI binary.

    The public npm package named ``kiro-cli`` is unrelated and does not expose a
    ``kiro-cli`` binary, so this pins the official installer path and global PATH
    copy that makes native Kiro work in managed sandboxes.
    """
    text = dockerfile.read_text()

    assert "https://cli.kiro.dev/install" in text
    assert "install -m 0755 /root/.local/bin/kiro-cli /usr/local/bin/kiro-cli" in text
    assert "      kiro-cli \\" not in text


@pytest.mark.parametrize(
    "dockerfile",
    [
        _ROOT / "deploy/docker/Dockerfile",
        _ROOT / "deploy/docker/Dockerfile.ubi",
    ],
)
def test_host_images_include_kiro_installer_dependency(dockerfile: Path) -> None:
    """Kiro's installer needs ``unzip`` on Linux."""
    text = dockerfile.read_text()
    assert "unzip" in text
