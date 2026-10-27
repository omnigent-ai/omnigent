"""The shared GitHub fetch, as both the CLI and the marketplace use it."""

from __future__ import annotations

import io
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
