"""Local Git exercises the clone's process and filesystem ownership boundaries."""

from __future__ import annotations

import json
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
    """A local repository that the example URL resolves to via ``insteadOf``."""
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
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{source.as_uri()}.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", REPOSITORY_URL)
    return source


@pytest.mark.parametrize(
    ("branch", "home_relative"),
    [(None, False), ("release", False), (None, True)],
    ids=["default-branch", "selected-branch", "home-relative"],
)
def test_clone_checks_out_the_requested_branch(
    local_repository: Path, tmp_path: Path, branch: str | None, home_relative: bool
) -> None:
    """The checkout lands at the requested path on the default or selected branch."""
    destination = tmp_path / "checkout"
    path = "~/checkout" if home_relative else str(destination)
    result = clone_repository(HostCloneRepositoryFrame("clone", REPOSITORY_URL, path, branch))
    assert result.status == "ok", result.error
    assert result.path == str(destination)
    current = subprocess.check_output(
        ["git", "-C", str(destination), "branch", "--show-current"], text=True
    ).strip()
    assert current == (branch or "main")


def test_clone_keeps_other_branches_for_later_worktrees(
    local_repository: Path, tmp_path: Path
) -> None:
    """A checkout cloned on ``main`` still exposes ``release`` to worktree creation."""
    destination = tmp_path / "checkout"
    result = clone_repository(HostCloneRepositoryFrame("clone", REPOSITORY_URL, str(destination)))
    assert result.status == "ok", result.error
    worktree = create_worktree(
        repo_path=str(destination),
        branch_name="feature/work",
        base_branch="origin/release",
    )
    assert (Path(worktree.worktree_path) / "README").read_text() == "initial checkout\n"


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_clone_refuses_an_existing_destination_unchanged(tmp_path: Path, kind: str) -> None:
    """An existing file, directory or symlink at the destination is never touched."""
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
    after = destination.lstat()
    assert (after.st_dev, after.st_ino) == (before.st_dev, before.st_ino)


@pytest.mark.parametrize(
    "path",
    ["relative/checkout", "~/..", "", "~no-such-user-here/checkout", "{tmp}/missing/x"],
    ids=["relative", "dotdot-leaf", "empty", "unknown-user", "missing-parent"],
)
def test_clone_rejects_an_unusable_destination(tmp_path: Path, path: str) -> None:
    """Relative, dot-leaf, empty, unresolvable and parentless paths are refused."""
    path = path.format(tmp=tmp_path)
    result = clone_repository(HostCloneRepositoryFrame("clone", REPOSITORY_URL, path))
    assert result.error_code == "invalid_input"


def test_failed_clone_removes_its_empty_reservation(
    local_repository: Path, tmp_path: Path
) -> None:
    """A git failure leaves no directory behind for the next attempt to trip over."""
    destination = tmp_path / "checkout"
    result = clone_repository(
        HostCloneRepositoryFrame("clone", REPOSITORY_URL, str(destination), "missing")
    )
    assert result.error_code == "clone_failed"
    assert not destination.exists()


def _replacing_popen(destination: Path, replacement: Path) -> type[subprocess.Popen]:
    """A Popen that swaps *replacement* in for *destination* once git has exited."""
    real_popen = subprocess.Popen

    class ReplacingPopen(real_popen):  # type: ignore[valid-type,misc]
        def wait(self, timeout: float | None = None) -> int:
            # ``Popen.__exit__`` waits again, so swap only on the first wait.
            first_wait = self.returncode is None
            returncode = super().wait(timeout=timeout)
            if first_wait:
                destination.rmdir()
                replacement.replace(destination)
            return returncode

    return ReplacingPopen


def test_failed_clone_keeps_a_replaced_directory_and_its_permissions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory swapped in after git exits is neither removed nor re-permissioned."""
    destination = tmp_path / "checkout"
    replacement = tmp_path / "replacement"
    replacement.mkdir(mode=0o750)
    (replacement / "keep").write_text("user content")
    monkeypatch.setattr(
        "omnigent.host.git_clone.subprocess.Popen",
        _replacing_popen(destination, replacement),
    )
    monkeypatch.setattr(
        "omnigent.host.git_clone.GitCloneOptions.clone_args",
        lambda self, branch: ["--definitely-not-a-git-option"],
    )
    result = clone_repository(HostCloneRepositoryFrame("clone", REPOSITORY_URL, str(destination)))
    assert result.error_code == "clone_failed"
    assert (destination / "keep").read_text() == "user content"
    assert destination.stat().st_mode & 0o777 == 0o750


def test_failed_clone_does_not_follow_a_symlink_swapped_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A symlink to a real directory swapped in after git exits is left alone."""
    destination = tmp_path / "checkout"
    target = tmp_path / "target"
    target.mkdir(mode=0o750)
    (target / "keep").write_text("user content")
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(
        "omnigent.host.git_clone.subprocess.Popen",
        _replacing_popen(destination, link),
    )
    monkeypatch.setattr(
        "omnigent.host.git_clone.GitCloneOptions.clone_args",
        lambda self, branch: ["--definitely-not-a-git-option"],
    )
    result = clone_repository(HostCloneRepositoryFrame("clone", REPOSITORY_URL, str(destination)))
    assert result.error_code == "clone_failed"
    assert destination.is_symlink()
    assert (target / "keep").read_text() == "user content"
    assert target.stat().st_mode & 0o777 == 0o750


@pytest.mark.skipif(os.name == "nt", reason="The executable fixture uses a POSIX shebang")
def test_clone_prompts_are_disabled_for_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Git and credential helpers get no terminal, askpass or interactive prompt."""
    tools = tmp_path / "tools"
    tools.mkdir()
    environment_record = tmp_path / "env.json"
    keys = ["GIT_TERMINAL_PROMPT", "GIT_ASKPASS", "SSH_ASKPASS_REQUIRE", "GCM_INTERACTIVE"]
    (tools / "git").write_text(
        f"#!{sys.executable}\nimport json, os\nfrom pathlib import Path\n"
        f"Path({str(environment_record)!r}).write_text("
        f"json.dumps({{key: os.environ.get(key) for key in {keys!r}}}))\n"
    )
    (tools / "git").chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    destination = tmp_path / "checkout"
    result = clone_repository(HostCloneRepositoryFrame("clone", REPOSITORY_URL, str(destination)))
    assert result.status == "ok", result.error
    recorded = json.loads(environment_record.read_text())
    assert recorded == {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS_REQUIRE": "never",
        "GCM_INTERACTIVE": "never",
    }


@pytest.mark.skipif(os.name == "nt", reason="The executable fixture uses a POSIX shebang")
def test_clone_timeout_stops_process_tree_and_keeps_partial_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out clone kills git's process tree and names the path to delete."""
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
    assert result.error is not None and str(destination) in result.error
    assert destination.is_dir()
    process_id = int(child_pid.read_text())
    # SIGKILL delivery is asynchronous; a surviving child would still be sleeping.
    poll_until(lambda: not _proc.process_alive(process_id), timeout_s=5, interval_s=0.05)
