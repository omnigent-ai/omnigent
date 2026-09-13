"""A package-built install must be able to run ``omnigent uninstall``.

When the sdist omits the top-level ``scripts/uninstall_oss.sh``, wheels
built from it ship without the uninstall script and ``omnigent uninstall``
fails with "uninstall script is missing from this installation".

Journey: install omnigent from the released package artifact -> run
``omnigent uninstall`` -> the CLI must locate its bundled uninstall script
instead of erroring.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from tests._helpers.build_python import python_with_setuptools

REPO_ROOT = Path(__file__).resolve().parents[2]
MISSING_SCRIPT_ERROR = "uninstall script is missing from this installation"


@pytest.mark.timeout(900)
def test_uninstall_finds_bundled_script_in_package_build(tmp_path: Path) -> None:
    """A package-built install must run `omnigent uninstall` without erroring."""
    build_python = python_with_setuptools()
    if build_python is None:
        pytest.skip("no python with setuptools available to build the package")

    build_env = os.environ.copy()
    # The uninstall script bundling is independent of the web UI bundle;
    # skipping the SPA build keeps this test hermetic and network-free.
    build_env["OMNIGENT_SKIP_WEB_UI"] = "true"

    # 1) Build the release sdist from the checkout (what a release publishes).
    # Regenerate egg metadata in a temp dir: a stale in-checkout
    # omnigent.egg-info/SOURCES.txt would otherwise feed the sdist manifest
    # and mask a manifest bug.
    dist_dir = tmp_path / "dist"
    egg_base = tmp_path / "egg-info"
    egg_base.mkdir()
    subprocess.run(
        [
            build_python,
            "setup.py",
            "-q",
            "egg_info",
            "--egg-base",
            str(egg_base),
            "sdist",
            "--dist-dir",
            str(dist_dir),
        ],
        cwd=REPO_ROOT,
        env=build_env,
        check=True,
        capture_output=True,
        timeout=600,
    )
    sdists = list(dist_dir.glob("omnigent-*.tar.gz"))
    assert len(sdists) == 1, f"expected exactly one sdist, got {sdists}"

    # 2) Build the wheel payload from that sdist, the way a from-sdist
    # release/pip build does (build_py populates build/lib).
    sdist_tree = tmp_path / "sdist_tree"
    sdist_tree.mkdir()
    with tarfile.open(sdists[0]) as archive:
        archive.extractall(sdist_tree, filter="data")
    (pkg_root,) = list(sdist_tree.iterdir())
    subprocess.run(
        [build_python, "setup.py", "-q", "build"],
        cwd=pkg_root,
        env=build_env,
        check=True,
        capture_output=True,
        timeout=600,
    )
    build_lib = pkg_root / "build" / "lib"
    assert (build_lib / "omnigent" / "cli.py").exists()

    # 3) Drive the real user command against the built payload: a fake HOME
    # whose state dir carries an installer-written installation_id (so an
    # install is detected), then `omnigent uninstall --dry-run`. The dry-run
    # resolves the uninstall script exactly like `--yes` does, without
    # mutating anything outside the fake HOME.
    home = tmp_path / "home"
    state_dir = home / ".omnigent"
    state_dir.mkdir(parents=True)
    (state_dir / "installation_id").write_text("e2e-test-install\n")

    cli_env = os.environ.copy()
    cli_env["HOME"] = str(home)
    cli_env["OMNIGENT_DATA_DIR"] = str(state_dir)
    bootstrap = (
        f"import sys; sys.path.insert(0, {str(build_lib)!r}); "
        "from omnigent.cli import main; main()"
    )
    result = subprocess.run(
        [sys.executable, "-c", bootstrap, "uninstall", "--dry-run"],
        cwd=tmp_path,
        env=cli_env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    output = result.stdout + result.stderr
    assert MISSING_SCRIPT_ERROR not in output, (
        f"package-built install cannot locate its uninstall script:\n{output}"
    )
    assert result.returncode == 0, (
        f"`omnigent uninstall --dry-run` exited {result.returncode}:\n{output}"
    )
