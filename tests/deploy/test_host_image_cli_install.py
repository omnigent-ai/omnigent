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
def test_host_images_install_pinned_kiro_cli(dockerfile: Path) -> None:
    """Managed host images must preinstall a *pinned* Kiro CLI binary.

    The public npm package named ``kiro-cli`` is unrelated and exposes no
    ``kiro-cli`` binary. Kiro's ``curl …/install`` script has no version flag
    (it always fetches ``latest``), so the images instead pull the immutable,
    versioned per-arch zip from the CDN, verify its sha256, and copy the binary
    onto the global PATH (see the pinning rationale in the Dockerfiles). This
    guards both that the pin stays in place and that the old unpinned installer
    never creeps back.
    """
    text = dockerfile.read_text()

    # Pinned to an explicit version, fetched from the immutable versioned CDN
    # path — not the unpinned ``cli.kiro.dev/install`` script, not ``…/latest/``.
    assert "ARG KIRO_CLI_VERSION=" in text
    assert "https://prod.download.cli.kiro.dev/stable/${KIRO_CLI_VERSION}/" in text
    assert "https://cli.kiro.dev/install" not in text
    # Integrity-checked, then copied onto the global PATH for all sandbox users.
    assert "sha256sum -c" in text
    assert "install -m 0755 /root/.local/bin/kiro-cli /usr/local/bin/kiro-cli" in text
    # kiro-cli is not an npm package, so it must not appear in the npm install list.
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


def test_extra_cli_rows_match_harness_install_table() -> None:
    """install-harness-cli.sh's npm + goose rows stay in sync with _HARNESS_INSTALL.

    The script resolves ``EXTRA_HARNESS_CLIS`` names to the same npm package
    and default pin the runtime installs via ``omnigent setup``, behind a
    "keep in sync" comment. A drift would bake a package or pin the runtime
    then rejects (opencode's runtime gate bounds 1.18.x), so assert the two
    tables agree instead of trusting the comment.
    """
    from omnigent.onboarding import harness_install as hi

    script = (_ROOT / "deploy/docker/install-harness-cli.sh").read_text()

    opencode = hi._HARNESS_INSTALL[hi.OPENCODE_KEY]
    assert opencode.package == "opencode-ai@~1.18.0"
    pkg, _, pin = opencode.package.rpartition("@")
    assert f"{pkg}@${{version:-{pin}}}" in script

    qwen = hi._HARNESS_INSTALL[hi.QWEN_KEY]
    assert qwen.package == "@qwen-code/qwen-code"
    assert f"{qwen.package}${{version:+@$version}}" in script

    # goose's default pin mirrors the runtime's minimum supported goose.
    assert f"${{1:-{hi._GOOSE_MIN_VERSION}}}" in script
