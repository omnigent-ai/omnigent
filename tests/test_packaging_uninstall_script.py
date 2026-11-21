"""Packaging must ship the uninstall script with every release artifact.

The uninstall script lives outside the package (top-level ``scripts/``), so
two packaging steps must both hold or ``omnigent uninstall`` dies at runtime
with "uninstall script is missing from this installation":

- the sdist must include ``scripts/uninstall_oss.sh`` (MANIFEST.in), and
- ``setup.py``'s ``_bundle_scripts`` must copy it into
  ``omnigent/resources/scripts/`` — and abort loudly when it can't, instead
  of silently shipping a wheel without it.

The test venv may not carry setuptools, so these tests drive ``setup.py``
through a setuptools-capable interpreter in a subprocess, the same way a
release build does.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import textwrap
from pathlib import Path

import pytest

from tests._helpers.build_python import python_with_setuptools

REPO_ROOT = Path(__file__).resolve().parents[1]

# Loads setup.py without running setup(), binds a _GenerateBuildInfo command
# to argv[2] as build_lib, and runs _bundle_scripts from argv[1].
_BUNDLE_DRIVER = textwrap.dedent(
    """
    import importlib.util
    import sys
    from unittest import mock

    from setuptools.dist import Distribution

    setup_dir, build_lib = sys.argv[1], sys.argv[2]
    spec = importlib.util.spec_from_file_location(
        "_omnigent_setup_under_test", setup_dir + "/setup.py"
    )
    module = importlib.util.module_from_spec(spec)
    with mock.patch("setuptools.setup"):
        spec.loader.exec_module(module)
    cmd = module._GenerateBuildInfo(Distribution())
    cmd.build_lib = build_lib
    cmd._bundle_scripts()
    """
)


def _run_bundle_scripts(
    python: str, setup_dir: Path, build_lib: Path
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [python, "-c", _BUNDLE_DRIVER, str(setup_dir), str(build_lib)],
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def build_python() -> str:
    python = python_with_setuptools()
    if python is None:
        pytest.skip("no python with setuptools available to run setup.py")
    return python


def test_bundle_scripts_copies_uninstall_script(build_python: str, tmp_path: Path) -> None:
    setup_dir = tmp_path / "src"
    (setup_dir / "scripts").mkdir(parents=True)
    shutil.copy2(REPO_ROOT / "setup.py", setup_dir / "setup.py")
    (setup_dir / "scripts" / "uninstall_oss.sh").write_text("#!/bin/sh\n")
    build_lib = tmp_path / "build_lib"

    result = _run_bundle_scripts(build_python, setup_dir, build_lib)

    assert result.returncode == 0, result.stderr
    bundled = build_lib / "omnigent" / "resources" / "scripts" / "uninstall_oss.sh"
    assert bundled.is_file()


def test_bundle_scripts_aborts_when_script_missing(build_python: str, tmp_path: Path) -> None:
    """A tree without the script must abort the build, not skip silently."""
    setup_dir = tmp_path / "src"
    setup_dir.mkdir()
    shutil.copy2(REPO_ROOT / "setup.py", setup_dir / "setup.py")
    build_lib = tmp_path / "build_lib"

    result = _run_bundle_scripts(build_python, setup_dir, build_lib)

    assert result.returncode != 0
    # Match the failure's meaning, not its exact wording: the abort must
    # name the script and say it can't be bundled.
    assert "uninstall_oss.sh" in result.stderr
    assert "cannot be bundled" in result.stderr
    assert not (build_lib / "omnigent" / "resources" / "scripts").exists()


def test_sdist_includes_uninstall_script(build_python: str, tmp_path: Path) -> None:
    """The sdist manifest must carry the top-level uninstall script."""
    env = os.environ.copy()
    # The uninstall script's packaging is independent of the web UI bundle;
    # skipping the SPA build keeps this test fast and network-free.
    env["OMNIGENT_SKIP_WEB_UI"] = "true"
    dist_dir = tmp_path / "dist"
    egg_base = tmp_path / "egg-info"
    egg_base.mkdir()
    subprocess.run(
        [
            build_python,
            "setup.py",
            "-q",
            # Regenerate egg metadata in a temp dir: a stale in-checkout
            # omnigent.egg-info/SOURCES.txt would otherwise feed the sdist
            # manifest and mask a broken MANIFEST.in.
            "egg_info",
            "--egg-base",
            str(egg_base),
            "sdist",
            "--dist-dir",
            str(dist_dir),
        ],
        cwd=REPO_ROOT,
        env=env,
        check=True,
        capture_output=True,
        timeout=600,
    )
    (sdist,) = dist_dir.glob("omnigent-*.tar.gz")
    with tarfile.open(sdist) as archive:
        members = {
            Path(name).relative_to(Path(name).parts[0]).as_posix() for name in archive.getnames()
        }
    assert "scripts/uninstall_oss.sh" in members
