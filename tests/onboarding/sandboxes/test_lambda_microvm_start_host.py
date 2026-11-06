"""
Tests for the Lambda MicroVM image's ``start_host.sh``.

The hooks server reruns the script when ``/resume`` restarts a host that stood
down, so a second run in the same VM must keep the first run's checkout and
start the host again instead of failing on the existing clone. The script also
installs the payload's host config and runs the host under the crash
supervisor, both rendered by the image's omnigent (here, this interpreter's).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

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


def _run_start_host(
    home: Path,
    bin_dir: Path,
    repo: Path | None = None,
    extra_env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = {
        "HOME": str(home),
        # The fake `omnigent` first; then this interpreter's directory so the
        # script's python3 can render the supervisor; then the rest.
        "PATH": os.pathsep.join(
            [str(bin_dir), str(Path(sys.executable).parent), os.environ.get("PATH", "")]
        ),
        "OMNIGENT_SERVER": "https://srv.example.com",
        **(extra_env or {}),
    }
    if repo is not None:
        env["OMNIGENT_REPO_URL"] = str(repo)
        env["OMNIGENT_REPO_NAME"] = "myrepo"
    return subprocess.run(
        ["bash", str(_SCRIPT)], env=env, capture_output=True, text=True, timeout=60
    )


def _fake_omnigent(bin_dir: Path, body: str) -> None:
    """Install a fake `omnigent` that runs the sh *body*."""
    fake = bin_dir / "omnigent"
    fake.write_text(f"#!/bin/sh\n{body}\n")
    fake.chmod(0o755)


def test_second_run_keeps_the_checkout_and_starts_the_host(
    tmp_path: Path, bare_repo: Path
) -> None:
    """A /resume restart reruns the script against the existing clone."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_omnigent(bin_dir, 'echo "fake omnigent $*"\nexit 0')

    first = _run_start_host(home, bin_dir, bare_repo)
    assert first.returncode == 0, first.stderr
    assert "fake omnigent host --server https://srv.example.com" in first.stdout
    marker = home / "workspace" / "myrepo" / "session-work.txt"
    marker.write_text("keep me\n")

    second = _run_start_host(home, bin_dir, bare_repo)
    assert second.returncode == 0, second.stderr
    assert "fake omnigent host --server https://srv.example.com" in second.stdout
    assert marker.read_text() == "keep me\n"


def test_a_crashed_host_is_restarted_by_the_supervisor(tmp_path: Path) -> None:
    """The host runs under supervise_host_command, so a crash restarts it.

    If the script could not render the supervisor it would start the host
    unsupervised, and a crash would leave a running MicroVM with no host.
    """
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    runs = tmp_path / "runs"
    # Crash on the first start, exit cleanly on the second.
    _fake_omnigent(
        bin_dir,
        f'echo run >> "{runs}"\n[ "$(wc -l < "{runs}")" -ge 2 ] && exit 0\nexit 1',
    )

    result = _run_start_host(home, bin_dir)

    assert result.returncode == 0, result.stderr
    assert runs.read_text().count("run") == 2
    assert "unsupervised" not in result.stderr


def test_host_config_json_is_installed_before_the_host_starts(tmp_path: Path) -> None:
    """OMNIGENT_HOST_CONFIG_JSON becomes the host's config.yaml.

    The payload carries only the config; the image's omnigent renders the shared
    write command, which must have run by the time the host reads its config.
    """
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    config_home = tmp_path / "config"
    seen = tmp_path / "seen.yaml"
    _fake_omnigent(bin_dir, f'cat "{config_home}/config.yaml" > "{seen}"\nexit 0')
    host_config = {
        "providers": {
            "litellm": {
                "kind": "gateway",
                "default": ["pi"],
                "openai": {"base_url": "http://litellm.example/v1", "wire_api": "chat"},
            }
        }
    }

    result = _run_start_host(
        home,
        bin_dir,
        extra_env={
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_HOST_CONFIG_JSON": json.dumps(host_config, separators=(",", ":")),
        },
    )

    assert result.returncode == 0, result.stderr
    assert yaml.safe_load(seen.read_text())["providers"] == host_config["providers"]


def test_unparseable_host_config_stops_the_start(tmp_path: Path) -> None:
    """A config that cannot be installed fails the start instead of starting a
    host without it."""
    home = tmp_path / "home"
    home.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    started = tmp_path / "started"
    _fake_omnigent(bin_dir, f'touch "{started}"\nexit 0')

    result = _run_start_host(
        home,
        bin_dir,
        extra_env={
            "OMNIGENT_CONFIG_HOME": str(tmp_path / "config"),
            "OMNIGENT_HOST_CONFIG_JSON": "{not json",
        },
    )

    assert result.returncode != 0
    assert not started.exists()
