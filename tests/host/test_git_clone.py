"""Local Git exercises the clone's process and filesystem ownership boundaries."""

from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from omnigent.host.frames import HostCloneRepositoryFrame
from omnigent.host.git_clone import clone_repository, validate_clone_path
from omnigent.host.git_worktree import create_worktree

REPOSITORY_URL = "https://git.example.com/team/sample.git"


@pytest.fixture()
def local_repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    subprocess.run(["git", "init", "-b", "main", str(source)], check=True, capture_output=True)
    (source / "README").write_text("initial checkout\n")
    subprocess.run(["git", "-C", str(source), "add", "README"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Example",
            "-c",
            "user.email=author@example.com",
            "commit",
            "-m",
            "Initial",
        ],
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
    result = clone_repository(
        HostCloneRepositoryFrame(
            "clone", REPOSITORY_URL, "~/checkout" if home_relative else str(destination), branch
        )
    )
    assert result.status == "ok", result.error
    assert result.path == str(destination)
    assert (destination / "README").read_text() == "initial checkout\n"
    assert subprocess.check_output(
        ["git", "-C", str(destination), "branch", "--show-current"], text=True
    ).strip() == (branch or "main")
    worktree = create_worktree(
        repo_path=str(destination),
        branch_name="feature/work",
        base_branch="origin/main" if branch else "origin/release",
    )
    assert (Path(worktree.worktree_path) / "README").read_text() == "initial checkout\n"


@pytest.mark.parametrize("kind", ["directory", "file", "symlink"])
def test_clone_never_replaces_an_existing_leaf(tmp_path: Path, kind: str) -> None:
    destination = tmp_path / "existing"
    if kind == "directory":
        destination.mkdir()
        (destination / "keep").write_text("keep")
    elif kind == "file":
        destination.write_text("keep")
    else:
        destination.symlink_to(tmp_path / "absent")
    before = destination.lstat()
    result = clone_repository(HostCloneRepositoryFrame("clone", REPOSITORY_URL, str(destination)))
    assert result.error_code == "clone_destination_conflict"
    assert destination.lstat().st_ino == before.st_ino


def test_failed_clone_removes_only_owned_checkout(local_repository: Path, tmp_path: Path) -> None:
    destination = tmp_path / "checkout"
    result = clone_repository(
        HostCloneRepositoryFrame("clone", REPOSITORY_URL, str(destination), "missing")
    )
    assert result.error_code == "clone_failed"
    assert not destination.exists()
    assert (local_repository / "README").exists()


@pytest.mark.skipif(os.name == "nt", reason="The executable fixture uses a POSIX shebang")
@pytest.mark.parametrize("setting", ["GIT_SSH_COMMAND", "GIT_SSH", "core.sshCommand"])
def test_clone_preserves_host_ssh_configuration(
    local_repository: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str
) -> None:
    record = tmp_path / "ssh.json"
    executable = tmp_path / "custom ssh"
    executable.write_text(
        f"#!{sys.executable}\nimport json, os, sys\nfrom pathlib import Path\n"
        f"Path({str(record)!r}).write_text(json.dumps({{"
        "'args': sys.argv[1:], 'stdin_is_tty': sys.stdin.isatty(), "
        "'prompt': os.environ.get('GIT_TERMINAL_PROMPT'), "
        "'askpass': os.environ.get('SSH_ASKPASS_REQUIRE')}))\nraise SystemExit(1)\n"
    )
    executable.chmod(0o755)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    monkeypatch.delenv("GIT_SSH", raising=False)
    monkeypatch.setenv("GIT_SSH_VARIANT", "ssh")
    if setting == "core.sshCommand":
        monkeypatch.setenv("GIT_CONFIG_COUNT", "2")
        monkeypatch.setenv("GIT_CONFIG_KEY_1", setting)
        monkeypatch.setenv("GIT_CONFIG_VALUE_1", shlex.quote(str(executable)))
    else:
        monkeypatch.setenv(
            setting, str(executable) if setting == "GIT_SSH" else shlex.quote(str(executable))
        )

    destination = tmp_path / "checkout"
    result = clone_repository(
        HostCloneRepositoryFrame("clone", "git@git.example.com:team/sample.git", str(destination))
    )
    assert result.error_code == "clone_failed"
    invocation = json.loads(record.read_text())
    assert "git@git.example.com" in invocation["args"]
    assert "-oBatchMode=yes" not in invocation["args"]
    assert invocation["stdin_is_tty"] is False
    assert invocation["prompt"] == "0"
    assert invocation["askpass"] == "never"
    assert not destination.exists()


@pytest.mark.parametrize("cleanup_allowed", [True, False])
def test_failed_clone_cleans_readonly_objects_or_logs_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    cleanup_allowed: bool,
) -> None:
    destination = tmp_path / "checkout"
    original_popen = subprocess.Popen
    original_unlink = os.unlink

    def failed_clone(*args: object, **kwargs: object) -> subprocess.Popen:
        git_object = destination / "object"
        git_object.write_text("incomplete Git object")
        git_object.chmod(stat.S_IRUSR)
        return original_popen([sys.executable, "-c", "raise SystemExit(1)"], **kwargs)

    def windows_unlink(path: str, *, dir_fd: int | None = None) -> None:
        # Model Windows' readonly attribute on POSIX, where readonly files are unlinkable.
        if Path(path).name == "object" and (
            not cleanup_allowed or not os.stat(path, dir_fd=dir_fd).st_mode & stat.S_IWUSR
        ):
            raise PermissionError("Cannot remove Git object")
        original_unlink(path, dir_fd=dir_fd)

    monkeypatch.setattr("omnigent.host.git_clone.subprocess.Popen", failed_clone)
    monkeypatch.setattr("os.unlink", windows_unlink)
    result = clone_repository(HostCloneRepositoryFrame("clone", REPOSITORY_URL, str(destination)))
    assert result.error_code == "clone_failed"
    assert destination.exists() is not cleanup_allowed
    assert (
        "Could not remove incomplete repository checkout" in caplog.text
    ) is not cleanup_allowed


@pytest.mark.parametrize("path", ["relative/path", "~another/checkout", "", "/tmp/bad\x00path"])
def test_clone_rejects_invalid_path(path: str) -> None:
    with pytest.raises(ValueError):
        validate_clone_path(path)


@pytest.mark.parametrize(
    "url,branch",
    [
        ("https://user:secret@git.example.com/team/sample.git", None),
        (REPOSITORY_URL + "#main", None),
        (REPOSITORY_URL, "--upload-pack=bad"),
    ],
)
def test_clone_rejects_input_before_reserving(
    tmp_path: Path, url: str, branch: str | None
) -> None:
    destination = tmp_path / "checkout"
    result = clone_repository(HostCloneRepositoryFrame("clone", url, str(destination), branch))
    assert result.error_code == "invalid_input"
    assert "secret" not in (result.error or "")
    assert not destination.exists()


@pytest.mark.skipif(os.name == "nt", reason="The executable fixture uses a POSIX shebang")
def test_clone_timeout_stops_process_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    child_pid = tmp_path / "child.pid"
    executable = tools / "git"
    child_command = (
        "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
    )
    executable.write_text(
        f"#!{sys.executable}\nimport subprocess, time\nfrom pathlib import Path\n"
        f"child = subprocess.Popen([{sys.executable!r}, '-c', {child_command!r}])\n"
        f"Path({str(child_pid)!r}).write_text(str(child.pid))\ntime.sleep(30)\n"
    )
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    monkeypatch.setattr("omnigent.host.git_clone.CLONE_TIMEOUT_S", 1)
    destination = tmp_path / "checkout"
    result = clone_repository(HostCloneRepositoryFrame("clone", REPOSITORY_URL, str(destination)))
    assert result.error_code == "clone_timeout"
    assert not destination.exists()
    process_id = int(child_pid.read_text())
    assert (
        not psutil.pid_exists(process_id)
        or psutil.Process(process_id).status() == psutil.STATUS_ZOMBIE
    )


def test_failed_clone_does_not_delete_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "checkout"
    original_popen = subprocess.Popen

    def replace_destination(*args: object, **kwargs: object) -> subprocess.Popen:
        destination.rename(tmp_path / "owned-checkout")
        destination.mkdir()
        (destination / "keep").write_text("user content")
        return original_popen([sys.executable, "-c", "raise SystemExit(1)"], **kwargs)

    monkeypatch.setattr("omnigent.host.git_clone.subprocess.Popen", replace_destination)
    result = clone_repository(HostCloneRepositoryFrame("clone", REPOSITORY_URL, str(destination)))
    assert result.error_code == "clone_failed"
    assert (destination / "keep").read_text() == "user content"
