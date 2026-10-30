"""The shared GitHub fetch, as both the CLI and the marketplace use it."""

from __future__ import annotations

import base64
import io
import os
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from omnigent.skills import git_source


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("acme/skills", ("https://github.com/acme/skills.git", None, "", None)),
        ("acme/skills@review", ("https://github.com/acme/skills.git", None, "", "review")),
        (
            "https://github.com/acme/skills/tree/v2/pack",
            ("https://github.com/acme/skills.git", "v2", "pack", None),
        ),
        ("github:acme/skills.git", ("https://github.com/acme/skills.git", None, "", None)),
        (
            "github:acme/skills/pack/sub@review",
            ("https://github.com/acme/skills.git", None, "pack/sub", "review"),
        ),
    ],
)
def test_parse_github_source(
    source: str, expected: tuple[str, str | None, str, str | None]
) -> None:
    parsed = git_source.parse_github_source(source)
    assert parsed is not None
    assert (parsed.clone_url, parsed.ref, parsed.subpath, parsed.name) == expected


@pytest.mark.parametrize("source", ["./local", "acme/../etc", "https://gitlab.com/a/b"])
def test_non_github_sources_are_rejected(source: str) -> None:
    assert git_source.parse_github_source(source) is None


def test_clone_is_hardened(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[tuple[list[str], dict[str, str]]] = []

    def run(
        cmd: list[str],
        env: dict[str, str],
        dest: Path,
        max_bytes: int | None,
        max_entries: int | None,
    ) -> object:
        seen.append((cmd, env))
        return subprocess.CompletedProcess(cmd, 128, "", "fatal: not found\n")

    monkeypatch.setattr(git_source, "_run_clone", run)
    with pytest.raises(git_source.GitSourceError, match=r"Could not clone .*: fatal: not found"):
        git_source.clone_github_source("https://github.com/a/b.git", "v1", tmp_path / "r")
    cmd, env = seen[0]
    assert {"core.hooksPath=/dev/null", "protocol.allow=never", "--no-recurse-submodules"} <= set(
        cmd
    )
    assert cmd[cmd.index("--branch") + 1] == "v1" and cmd[-3] == "--"
    assert env["GIT_TERMINAL_PROMPT"] == "0" and env["GIT_LFS_SKIP_SMUDGE"] == "1"

    # A hex-looking ref is still passed through as a branch; the failure explains why.
    with pytest.raises(git_source.GitSourceError, match="Only branch and tag names"):
        git_source.clone_github_source("https://github.com/a/b.git", "deadbee", tmp_path / "r2")
    cmd, _env = seen[1]
    assert cmd[cmd.index("--branch") + 1] == "deadbee"


def test_tar_untrusted_dir_refuses_symlinks_and_lfs(tmp_path: Path) -> None:
    src = tmp_path / "skill"
    src.mkdir()
    (src / "SKILL.md").write_text("---\nname: x\ndescription: y\n---\n")
    (src / ".git").mkdir()
    (src / ".git" / "HEAD").write_text("ref")
    data = git_source.tar_untrusted_dir(src, what="a skill")
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        assert tf.getnames() == ["SKILL.md"]

    (src / "link").symlink_to(tmp_path)
    with pytest.raises(git_source.GitSourceError, match="a skill with symlinks"):
        git_source.tar_untrusted_dir(src, what="a skill")
    (src / "link").unlink()

    (src / "model.bin").write_bytes(git_source.LFS_POINTER + b"\n")
    with pytest.raises(git_source.GitSourceError, match="Git LFS"):
        git_source.tar_untrusted_dir(src, what="a skill")


def test_isolated_clone_drops_inherited_git_config_and_helpers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The operator's credential helper, global config and home directory
    # (.netrc, .config/git) must not serve a marketplace user's request.
    operator_home = tmp_path / "op"
    operator_home.mkdir()
    (operator_home / ".netrc").write_text("machine github.com login op password secret\n")
    monkeypatch.setenv("HOME", str(operator_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(operator_home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/home/op/.gitconfig")
    monkeypatch.setenv("GIT_ASKPASS", "/usr/bin/op-askpass")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/op-agent")
    monkeypatch.setenv("PATH", "/usr/bin")
    seen: list[tuple[list[str], dict[str, str], list[str]]] = []

    def run(
        cmd: list[str],
        env: dict[str, str],
        dest: Path,
        max_bytes: int | None,
        max_entries: int | None,
    ) -> object:
        seen.append((cmd, env, os.listdir(env["HOME"])))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(git_source, "_run_clone", run)
    git_source.clone_github_source(
        "https://github.com/a/b.git", None, tmp_path / "r", isolated=True
    )
    cmd, env, home_listing = seen[0]
    assert cmd[cmd.index("credential.helper=") - 1] == "-c"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_ASKPASS"] == "" and "SSH_AUTH_SOCK" not in env
    assert env["PATH"] == "/usr/bin"
    # An empty home of its own, gone again after the clone.
    assert env["HOME"] != str(operator_home) and env["XDG_CONFIG_HOME"] == env["HOME"]
    assert home_listing == [] and not os.path.exists(env["HOME"])

    # The CLI keeps the developer's own setup.
    git_source.clone_github_source("https://github.com/a/b.git", None, tmp_path / "r2")
    _cmd, env, _listing = seen[1]
    assert env["GIT_CONFIG_GLOBAL"] == "/home/op/.gitconfig" and "credential.helper=" not in _cmd
    assert env["HOME"] == str(operator_home)


def test_tar_untrusted_dir_stops_at_the_limits_before_reading_more(tmp_path: Path) -> None:
    src = tmp_path / "skill"
    src.mkdir()
    (src / "SKILL.md").write_text("---\nname: x\ndescription: y\n---\n")
    (src / "big.bin").write_bytes(b"\0" * 4096)
    with pytest.raises(git_source.GitSourceError, match="larger than"):
        git_source.tar_untrusted_dir(src, what="a skill", max_bytes=1024)
    with pytest.raises(git_source.GitSourceError, match="more than 1 files"):
        git_source.tar_untrusted_dir(src, what="a skill", max_entries=1)
    # Within the limits the archive is produced.
    assert git_source.tar_untrusted_dir(src, what="a skill", max_bytes=8192, max_entries=5)


def test_enterprise_hosts_the_server_knows_keep_their_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("GH_HOST", raising=False)
    monkeypatch.setenv("OMNIGENT_GIT_PROVIDER_GITHUB_HOSTS", "github.acme.internal")
    parsed = git_source.parse_github_source("https://github.acme.internal/team/skills/tree/v2")
    assert parsed is not None
    assert (parsed.host, parsed.clone_url, parsed.ref) == (
        "github.acme.internal",
        "https://github.acme.internal/team/skills.git",
        "v2",
    )
    # An unknown host is refused, never normalised onto github.com.
    assert git_source.parse_github_source("https://github.other.internal/team/skills") is None
    monkeypatch.setenv("GH_HOST", "github.other.internal")
    assert git_source.parse_github_source("https://github.other.internal/team/skills") is not None

    assert git_source.repository_label("https://github.com/acme/skills.git") == "acme/skills"
    assert (
        git_source.repository_label("https://github.acme.internal/team/skills.git")
        == "github.acme.internal/team/skills"
    )


def test_token_travels_in_the_environment_not_the_command_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[tuple[list[str], dict[str, str]]] = []

    def run(
        cmd: list[str],
        env: dict[str, str],
        dest: Path,
        max_bytes: int | None,
        max_entries: int | None,
    ) -> object:
        seen.append((cmd, env))
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(git_source, "_run_clone", run)
    git_source.clone_github_source(
        "https://github.acme.internal/team/skills.git",
        None,
        tmp_path / "r",
        isolated=True,
        token="ghs_secret",
    )
    cmd, env = seen[0]
    assert not any("ghs_secret" in part for part in cmd)
    assert env["GIT_CONFIG_COUNT"] == "1"
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.acme.internal/.extraheader"
    basic = base64.b64encode(b"x-access-token:ghs_secret").decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {basic}"
    # The isolation still holds around the header.
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull and "credential.helper=" in cmd

    git_source.clone_github_source(
        "https://github.com/a/b.git", None, tmp_path / "r2", isolated=True
    )
    assert "GIT_CONFIG_COUNT" not in seen[1][1]


class _HangingGit:
    """A git that never finishes: every wait times out until it is killed."""

    returncode: int | None = None

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.killed = False

    def communicate(self, timeout: float | None = None) -> tuple[str, str]:
        if self.killed:
            self.returncode = -9
            return "", ""
        raise subprocess.TimeoutExpired("git", timeout or 0)

    def kill(self) -> None:
        self.killed = True


def _no_git(*_args: object, **_kwargs: object) -> None:
    raise FileNotFoundError("git")


@pytest.mark.parametrize(
    ("popen", "message"),
    [(_no_git, "needs git on your PATH"), (_HangingGit, "Timed out cloning")],
)
def test_clone_reports_a_missing_or_hanging_git(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, popen: object, message: str
) -> None:
    monkeypatch.setattr(git_source.subprocess, "Popen", popen)
    monkeypatch.setattr(git_source, "CLONE_TIMEOUT_S", 0.0)
    monkeypatch.setattr(git_source, "CHECKOUT_POLL_S", 0.01)
    with pytest.raises(git_source.GitSourceError, match=message):
        git_source.clone_github_source("https://github.com/a/b.git", None, tmp_path / "r")


def test_a_clone_is_killed_once_it_exceeds_its_byte_budget(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A stand-in for git that keeps writing into the destination: the watchdog
    # stops it at the budget instead of letting it fill the disk.
    fake_git = tmp_path / "fake-git.py"
    fake_git.write_text(
        f"#!{sys.executable}\n"
        "import os, sys, time\n"
        "dest = sys.argv[-1]\n"
        "os.makedirs(dest)\n"
        "with open(os.path.join(dest, 'blob'), 'ab') as fh:\n"
        "    for _ in range(400):\n"
        "        fh.write(b'x' * 65536); fh.flush(); time.sleep(0.01)\n"
    )
    fake_git.chmod(0o755)
    monkeypatch.setattr(git_source, "GIT", str(fake_git))
    monkeypatch.setattr(git_source, "CHECKOUT_POLL_S", 0.05)
    dest = tmp_path / "checkout"
    started = time.monotonic()
    with pytest.raises(git_source.GitSourceError, match="exceeds 1 MB on disk"):
        git_source.clone_github_source(
            str(fake_git), None, dest, isolated=True, max_bytes=1024 * 1024
        )
    assert time.monotonic() - started < 10
    assert git_source._tree_usage(dest).bytes < 4 * 1024 * 1024

    # Within the budget the stand-in finishes and the clone succeeds.
    small = tmp_path / "small-git.py"
    small.write_text(f"#!{sys.executable}\nimport os, sys\nos.makedirs(sys.argv[-1])\n")
    small.chmod(0o755)
    monkeypatch.setattr(git_source, "GIT", str(small))
    git_source.clone_github_source(
        str(small), None, tmp_path / "ok", isolated=True, max_bytes=1024
    )


def _stand_in_git(path: Path, body: str) -> Path:
    """A script run in git's place; it receives git's arguments and the destination last."""
    path.write_text(f"#!{sys.executable}\nimport os, sys\ndest = sys.argv[-1]\n{body}")
    path.chmod(0o755)
    return path


def test_a_finished_checkout_over_budget_is_refused_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Crossing the budget and exiting before the next poll must not slip through.
    fast = _stand_in_git(
        tmp_path / "fast-git.py",
        "os.makedirs(dest)\n"
        "open(os.path.join(dest, 'blob'), 'wb').write(b'x' * (2 * 1024 * 1024))\n",
    )
    monkeypatch.setattr(git_source, "GIT", str(fast))
    monkeypatch.setattr(git_source, "CHECKOUT_POLL_S", 5.0)
    with pytest.raises(git_source.GitSourceError, match="exceeds 1 MB on disk"):
        git_source.clone_github_source(
            str(fast), None, tmp_path / "checkout", isolated=True, max_bytes=1024 * 1024
        )


def test_a_checkout_with_too_many_entries_is_refused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # Inodes are a budget too: thousands of empty files and directories weigh
    # nothing in bytes and would otherwise pass.
    many = _stand_in_git(
        tmp_path / "many-git.py",
        "import time\n"
        "for i in range(300):\n"
        "    os.makedirs(os.path.join(dest, f'd{i}'))\n"
        "    open(os.path.join(dest, f'd{i}', 'f'), 'w').close()\n"
        "    time.sleep(0.002)\n",
    )
    monkeypatch.setattr(git_source, "GIT", str(many))
    monkeypatch.setattr(git_source, "CHECKOUT_POLL_S", 0.05)
    with pytest.raises(git_source.GitSourceError, match="more than 100 files and directories"):
        git_source.clone_github_source(
            str(many), None, tmp_path / "checkout", isolated=True, max_entries=100
        )
    assert git_source._tree_usage(tmp_path / "checkout").entries < 600
