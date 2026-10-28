"""The shared GitHub fetch, as both the CLI and the marketplace use it."""

from __future__ import annotations

import base64
import io
import os
import subprocess
import tarfile
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

    def run(cmd: list[str], **kwargs: object) -> object:
        seen.append((cmd, kwargs["env"]))  # type: ignore[arg-type]
        return type("P", (), {"returncode": 128, "stderr": "fatal: not found\n"})()

    monkeypatch.setattr(subprocess, "run", run)
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

    def run(cmd: list[str], **kwargs: object) -> object:
        env = kwargs["env"]  # type: ignore[index]
        seen.append((cmd, env, os.listdir(env["HOME"])))  # type: ignore[arg-type]
        return type("P", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(subprocess, "run", run)
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

    def run(cmd: list[str], **kwargs: object) -> object:
        seen.append((cmd, kwargs["env"]))  # type: ignore[arg-type]
        return type("P", (), {"returncode": 0, "stderr": ""})()

    monkeypatch.setattr(subprocess, "run", run)
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
