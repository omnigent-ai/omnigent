"""Local Git exercises the clone's process and filesystem ownership boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.host.frames import HostCloneRepositoryFrame
from omnigent.host.git_clone import clone_repository
from omnigent.host.git_worktree import create_worktree
from omnigent.inner import _proc
from tests._helpers.polling import poll_until

REPOSITORY_URL = "https://git.example.com/team/sample.git"


@pytest.fixture()
def local_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "source"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
    (source / "README").write_text("initial checkout\n")
    subprocess.run(["git", "-C", str(source), "add", "README"], check=True)
    author = ["-c", "user.name=Example", "-c", "user.email=a@example.com"]
    subprocess.run(
        ["git", "-C", str(source), *author, "commit", "-m", "Initial"],
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "-C", str(source), "branch", "release"], check=True)
    # Exercise a real clone without credentials or network access.
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{source.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", REPOSITORY_URL)
    return source


@pytest.mark.parametrize("branch,home_relative", [(None, False), ("release", False), (None, True)])
def test_clone_local_repository(
    local_repository: Path, tmp_path: Path, branch: str | None, home_relative: bool
) -> None:
    destination = tmp_path / "checkout"
    path = "~/checkout" if home_relative else str(destination)
    result = clone_repository(HostCloneRepositoryFrame("clone", REPOSITORY_URL, path, branch))
    assert result.status == "ok", result.error
    assert result.path == str(destination)
    assert subprocess.check_output(
        ["git", "-C", str(destination), "branch", "--show-current"], text=True
    ).strip() == (branch or "main")
    # Other remote branches stay available for later session worktrees.
    worktree = create_worktree(
        repo_path=str(destination),
        branch_name="feature/work",
        base_branch="origin/main" if branch else "origin/release",
    )
    assert (Path(worktree.worktree_path) / "README").read_text() == "initial checkout\n"


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_clone_never_replaces_an_existing_destination(tmp_path: Path, kind: str) -> None:
    destination = tmp_path / "existing"
    if kind == "directory":
        destination.mkdir()
    elif kind == "file":
        destination.write_text("keep")
    else:
        destination.symlink_to(tmp_path / "absent")
    before = destination.lstat()
    result = clone_repository(HostCloneRepositoryFrame("clone", REPOSITORY_URL, str(destination)))
    assert result.error_code == "clone_destination_conflict"
    assert destination.lstat().st_ino == before.st_ino


@pytest.mark.parametrize("path", ["relative/checkout", "~/..", ""])
def test_clone_rejects_a_non_absolute_destination(tmp_path: Path, path: str) -> None:
    result = clone_repository(HostCloneRepositoryFrame("clone", REPOSITORY_URL, path))
    assert result.error_code == "invalid_input"


def test_failed_clone_removes_only_its_own_checkout(
    local_repository: Path, tmp_path: Path
) -> None:
    destination = tmp_path / "checkout"
    result = clone_repository(
        HostCloneRepositoryFrame("clone", REPOSITORY_URL, str(destination), "missing")
    )
    assert result.error_code == "clone_failed"
    assert not destination.exists()
    assert (local_repository / "README").exists()


def test_failed_clone_keeps_a_replaced_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "checkout"
    original_popen = subprocess.Popen

    def replace_destination(*args: object, **kwargs: object) -> subprocess.Popen:
        destination.rename(tmp_path / "reserved")
        destination.mkdir()
        (destination / "keep").write_text("user content")
        return original_popen([sys.executable, "-c", "raise SystemExit(1)"], **kwargs)

    monkeypatch.setattr("omnigent.host.git_clone.subprocess.Popen", replace_destination)
    result = clone_repository(HostCloneRepositoryFrame("clone", REPOSITORY_URL, str(destination)))
    assert result.error_code == "clone_failed"
    assert (destination / "keep").read_text() == "user content"


@pytest.mark.skipif(os.name == "nt", reason="The executable fixture uses a POSIX shebang")
def test_clone_timeout_stops_process_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    child_pid = tmp_path / "child.pid"
    child_command = (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    )
    (tools / "git").write_text(
        f"#!{sys.executable}\nimport subprocess, time\nfrom pathlib import Path\n"
        f"child = subprocess.Popen([{sys.executable!r}, '-c', {child_command!r}])\n"
        f"Path({str(child_pid)!r}).write_text(str(child.pid))\ntime.sleep(30)\n"
    )
    (tools / "git").chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr("omnigent.host.git_clone.CLONE_TIMEOUT_S", 2)
    destination = tmp_path / "checkout"
    result = clone_repository(HostCloneRepositoryFrame("clone", REPOSITORY_URL, str(destination)))
    assert result.error_code == "clone_timeout"
    assert not destination.exists()
    process_id = int(child_pid.read_text())
    # SIGKILL delivery is asynchronous; a surviving child would still be sleeping.
    poll_until(lambda: not _proc.process_alive(process_id), timeout_s=5, interval_s=0.05)
