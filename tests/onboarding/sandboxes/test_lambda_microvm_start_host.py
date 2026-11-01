"""
Tests for the Lambda MicroVM image's ``start_host.sh``.

The hooks server reruns the script when ``/resume`` restarts a host that stood
down, so a second run in the same VM must keep the first run's checkout and
start the host again instead of failing on the existing clone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[3] / "deploy" / "aws-lambda-microvm" / "start_host.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("git") is None,
    reason="start_host.sh needs bash and git",
)


def _git(*args: str, cwd: Path | None = None) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
    )


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """A local repository the script can clone, with one commit on main."""
    work = tmp_path / "src"
    work.mkdir()
    _git("init", "-q", "-b", "main", cwd=work)
    (work / "README").write_text("hello\n")
    _git("add", "README", cwd=work)
    _git("commit", "-q", "-m", "init", cwd=work)
    bare = tmp_path / "myrepo.git"
    _git("clone", "-q", "--bare", str(work), str(bare))
    return bare


def _run_start_host(home: Path, bin_dir: Path, repo: Path) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str(home),
        # The fake `omnigent` first; then this interpreter's directory so the
        # script's python3 can render the supervisor; then the rest.
        "PATH": os.pathsep.join(
            [str(bin_dir), str(Path(sys.executable).parent), os.environ.get("PATH", "")]
        ),
        "OMNIGENT_SERVER": "https://srv.example.com",
        "OMNIGENT_REPO_URL": str(repo),
        "OMNIGENT_REPO_NAME": "myrepo",
    }
    return subprocess.run(
        ["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )


def test_second_run_keeps_the_checkout_and_starts_the_host(
    tmp_path: Path, bare_repo: Path
) -> None:
    """A /resume restart reruns the script against the existing clone."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "omnigent"
    fake.write_text('#!/bin/sh\necho "fake omnigent $*"\nexit 0\n')
    fake.chmod(0o755)

    first = _run_start_host(home, bin_dir, bare_repo)
    assert first.returncode == 0, first.stderr
    assert "fake omnigent host --server https://srv.example.com" in first.stdout
    marker = home / "workspace" / "myrepo" / "session-work.txt"
    marker.write_text("keep me\n")

    second = _run_start_host(home, bin_dir, bare_repo)
    assert second.returncode == 0, second.stderr
    assert "fake omnigent host --server https://srv.example.com" in second.stdout
    assert marker.read_text() == "keep me\n"
