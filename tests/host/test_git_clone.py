"""Tests for :mod:`omnigent.host.git_clone`.

Clones run real git against a local bare repository. Git's ``insteadOf``
rewrite, set through ``GIT_CONFIG_*`` env vars, maps an ``https://`` URL onto
that bare repository so the accepted-URL check stays in force without network.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import psutil  # type: ignore[import-untyped]
import pytest

from omnigent.host import git_clone
from omnigent.host.git_clone import CloneError, clone_repository

_REMOTE_BASE = "https://git.example.test/"
_URL = f"{_REMOTE_BASE}acme/widgets.git"


def _git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` with a fixed identity.

    :param cwd: Directory to run in.
    :param args: Git arguments, e.g. ``("init", "-q")``.
    :returns: Stripped stdout.
    """
    completed = subprocess.run(
        [
            "git",
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.test",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture()
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Serve ``_URL`` from a one-commit local bare repository.

    :param tmp_path: Pytest temp dir.
    :param monkeypatch: Used to set git's ``insteadOf`` rewrite.
    :returns: Directory holding ``acme/widgets.git``.
    """
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-q", "-b", "main")
    (source / "README.md").write_text("widgets\n")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "init")
    served = tmp_path / "served"
    (served / "acme").mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "--bare", str(source), str(served / "acme" / "widgets.git"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", f"url.{served.as_uri()}/.insteadOf")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", _REMOTE_BASE)
    return served


def test_clone_creates_missing_parent_and_checks_out(remote: Path, tmp_path: Path) -> None:
    """A fresh clone lands in ``<parent>/<name>`` and keeps the requested remote."""
    parent = tmp_path / "code" / "nested"

    cloned = clone_repository(url=_URL, parent_directory=str(parent), directory_name="widgets")

    assert cloned.path == str(parent / "widgets")
    assert cloned.reused_existing is False
    assert (parent / "widgets" / "README.md").read_text() == "widgets\n"
    assert _git(parent / "widgets", "config", "--get", "remote.origin.url") == _URL


def test_clone_expands_home(remote: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``~`` parent resolves against the host user's home."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    cloned = clone_repository(url=_URL, parent_directory="~/src", directory_name="widgets")

    assert cloned.path == str(tmp_path / "home" / "src" / "widgets")


@pytest.mark.parametrize("retry_url", [_URL, f"{_REMOTE_BASE}acme/widgets/"])
def test_retry_reuses_checkout_of_same_remote(
    remote: Path, tmp_path: Path, retry_url: str
) -> None:
    """Re-cloning the same remote links the existing checkout instead of failing."""
    first = clone_repository(url=_URL, parent_directory=str(tmp_path), directory_name="widgets")
    (Path(first.path) / "local-change.txt").write_text("keep me")

    again = clone_repository(
        url=retry_url, parent_directory=str(tmp_path), directory_name="widgets"
    )

    assert again.path == first.path
    assert again.reused_existing is True
    assert (Path(first.path) / "local-change.txt").read_text() == "keep me"


def test_unfinished_clone_of_same_remote_is_a_conflict(remote: Path, tmp_path: Path) -> None:
    """A checkout of the remote with no commit (a killed clone) is not reused."""
    unfinished = tmp_path / "widgets"
    unfinished.mkdir()
    _git(unfinished, "init", "-q")
    _git(unfinished, "remote", "add", "origin", _URL)

    with pytest.raises(CloneError, match="no commits checked out") as raised:
        clone_repository(url=_URL, parent_directory=str(tmp_path), directory_name="widgets")

    assert raised.value.conflict is True
    assert (unfinished / ".git").is_dir()


def test_retry_of_empty_repository_clone_is_a_conflict(remote: Path, tmp_path: Path) -> None:
    """An empty remote clones once; a retry can't tell it from an interrupted clone."""
    _git(tmp_path, "init", "-q", "--bare", str(remote / "acme" / "empty.git"))
    url = f"{_REMOTE_BASE}acme/empty.git"
    parent = tmp_path / "code"

    first = clone_repository(url=url, parent_directory=str(parent), directory_name="empty")
    assert first.reused_existing is False
    with pytest.raises(CloneError, match="an empty repository") as raised:
        clone_repository(url=url, parent_directory=str(parent), directory_name="empty")

    assert raised.value.conflict is True
    assert (parent / "empty" / ".git").is_dir()


def test_existing_unrelated_folder_is_a_conflict_and_untouched(
    remote: Path, tmp_path: Path
) -> None:
    """A taken destination that is not this remote is reported, never overwritten."""
    taken = tmp_path / "widgets"
    taken.mkdir()
    (taken / "notes.txt").write_text("mine")

    with pytest.raises(CloneError) as raised:
        clone_repository(url=_URL, parent_directory=str(tmp_path), directory_name="widgets")

    assert raised.value.conflict is True
    assert f"{taken} already exists" in raised.value.message
    assert (taken / "notes.txt").read_text() == "mine"


def test_subfolder_of_another_checkout_is_a_conflict(remote: Path, tmp_path: Path) -> None:
    """A plain folder inside a checkout of the remote is not that checkout's root."""
    outer = clone_repository(url=_URL, parent_directory=str(tmp_path), directory_name="widgets")
    inner = Path(outer.path) / "widgets"
    inner.mkdir()

    with pytest.raises(CloneError) as raised:
        clone_repository(url=_URL, parent_directory=outer.path, directory_name="widgets")

    assert raised.value.conflict is True


def test_git_failure_reports_summarized_stderr(remote: Path, tmp_path: Path) -> None:
    """A missing repository fails with git's reason and leaves no folder behind."""
    with pytest.raises(CloneError) as raised:
        clone_repository(
            url=f"{_REMOTE_BASE}acme/missing.git",
            parent_directory=str(tmp_path),
            directory_name="missing",
        )

    assert raised.value.conflict is False
    assert raised.value.message.startswith("fatal:")
    assert "Cloning into" not in raised.value.message
    assert not (tmp_path / "missing").exists()


def test_timeout_removes_partial_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A clone killed at its budget does not leave a half-written folder."""

    def _slow_git(args: list[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
        Path(args[-1]).mkdir()
        raise subprocess.TimeoutExpired(cmd="git clone", timeout=timeout_s)

    monkeypatch.setattr(git_clone, "_run_git", _slow_git)

    with pytest.raises(CloneError, match="timed out after 5s"):
        clone_repository(
            url=_URL, parent_directory=str(tmp_path), directory_name="widgets", timeout_s=5
        )

    assert not (tmp_path / "widgets").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="fake git is a POSIX shell script")
def test_timeout_kills_git_transport_helpers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timed-out git takes its helper processes (ssh, remote-https) down with it."""
    helper_pid_file = tmp_path / "helper.pid"
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_git = fake_bin / "git"
    sleep = shutil.which("sleep")
    fake_git.write_text(f"#!/bin/sh\n{sleep} 60 &\necho $! > {helper_pid_file}\nwait\n")
    fake_git.chmod(0o755)
    monkeypatch.setenv("PATH", str(fake_bin))

    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        git_clone._run_git(["clone"], timeout_s=1)

    assert time.monotonic() - started < 30
    helper_pid = int(helper_pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            if psutil.Process(helper_pid).status() == psutil.STATUS_ZOMBIE:
                break
        except psutil.NoSuchProcess:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"git helper {helper_pid} outlived the timed-out clone")


def test_concurrent_clone_into_same_destination_is_a_conflict(tmp_path: Path) -> None:
    """A second clone into a path that is still being written is refused."""
    destination = str(tmp_path / "widgets")
    with git_clone._claim_destination(destination), pytest.raises(CloneError) as raised:
        clone_repository(url=_URL, parent_directory=str(tmp_path), directory_name="widgets")

    assert raised.value.conflict is True
    assert "already in progress" in raised.value.message


@pytest.mark.parametrize(
    ("url", "parent", "name", "message"),
    [
        ("ext::sh -c touch% /tmp/pwned", "/work", "repo", "https:// or git@"),
        ("/srv/git/repo.git", "/work", "repo", "https:// or git@"),
        ("git@-oProxyCommand=evil:acme/repo", "/work", "repo", "https:// or git@"),
        ("https://-x/acme/repo", "/work", "repo", "https:// or git@"),
        (_URL, "/work", "../escape", "not a usable folder name"),
        (_URL, "/work", "..", "not a usable folder name"),
        (_URL, "relative/dir", "repo", "absolute or ~ path"),
    ],
)
def test_rejects_unsafe_input_without_running_git(
    monkeypatch: pytest.MonkeyPatch, url: str, parent: str, name: str, message: str
) -> None:
    """Inputs that could reach another transport or path never run git."""

    def _no_git(args: list[str], *, timeout_s: float) -> subprocess.CompletedProcess[str]:
        raise AssertionError(f"git ran for {args}")

    monkeypatch.setattr(git_clone, "_run_git", _no_git)

    with pytest.raises(CloneError, match=message):
        clone_repository(url=url, parent_directory=parent, directory_name=name)


def test_git_runs_without_prompts() -> None:
    """Credential prompts are disabled so a missing login fails fast."""
    env = git_clone._git_env()

    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GCM_INTERACTIVE"] == "never"
