"""Private home and session-owned skill staging safety."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest

from omnigent.inner.codex_staging import (
    CODEX_SKILLS_PREFIX,
    _staging_root_path,
    codex_home_staging_root,
    prepare_codex_skills_dir,
)


@pytest.fixture
def isolated_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def test_staging_root_is_private_and_under_tempdir(isolated_tempdir: Path) -> None:
    root = codex_home_staging_root()
    assert root.parent == isolated_tempdir
    assert root.is_dir()
    if hasattr(os, "getuid"):
        assert f"-{os.getuid()}" in root.name
        assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_staging_root_tightens_a_loose_preexisting_mode(isolated_tempdir: Path) -> None:
    root = codex_home_staging_root()
    root.chmod(0o770)
    assert stat.S_IMODE(codex_home_staging_root().stat().st_mode) == 0o700


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX directory symlinks")
def test_staging_root_resolves_symlinked_temp_ancestors(
    isolated_tempdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_temp = isolated_tempdir / "real-temp"
    real_temp.mkdir()
    temp_alias = isolated_tempdir / "temp-alias"
    temp_alias.symlink_to(real_temp, target_is_directory=True)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(temp_alias))
    assert codex_home_staging_root().parent == real_temp.resolve()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership semantics")
def test_staging_root_refuses_a_symlink_squatting_its_name(isolated_tempdir: Path) -> None:
    outside = isolated_tempdir / "outside"
    outside.mkdir()
    _staging_root_path().symlink_to(outside)
    with pytest.raises(OSError):
        codex_home_staging_root()


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership semantics")
def test_staging_root_refuses_a_root_owned_by_another_user(
    isolated_tempdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign_uid = os.getuid() + 1
    monkeypatch.setattr(os, "getuid", lambda: foreign_uid)
    with pytest.raises(OSError):
        codex_home_staging_root()


def test_skills_refresh_preserves_mount_root_and_removes_old_contents(tmp_path: Path) -> None:
    root = tmp_path / f"{CODEX_SKILLS_PREFIX}session"
    root.mkdir(mode=0o700)
    skill = root / "old-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text("old content")
    identity = root.stat().st_ino

    assert prepare_codex_skills_dir(root) == root.resolve()
    assert root.stat().st_ino == identity
    assert list(root.iterdir()) == []


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX directory symlinks")
def test_skills_refresh_does_not_follow_child_symlinks(tmp_path: Path) -> None:
    root = tmp_path / f"{CODEX_SKILLS_PREFIX}session"
    root.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep")
    (root / "link").symlink_to(outside, target_is_directory=True)

    prepare_codex_skills_dir(root)

    assert marker.read_text() == "keep"
    assert list(root.iterdir()) == []


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX directory symlinks")
def test_skills_refresh_rejects_symlink_root(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o700)
    marker = outside / "keep.txt"
    marker.write_text("keep")
    root = tmp_path / f"{CODEX_SKILLS_PREFIX}session"
    root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(OSError):
        prepare_codex_skills_dir(root)
    assert marker.read_text() == "keep"


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX permission semantics")
@pytest.mark.parametrize("mode", [0o750, 0o770, 0o707])
def test_skills_refresh_rejects_nonprivate_root(tmp_path: Path, mode: int) -> None:
    root = tmp_path / f"{CODEX_SKILLS_PREFIX}session"
    root.mkdir(mode=mode)
    root.chmod(mode)
    with pytest.raises(OSError):
        prepare_codex_skills_dir(root)


def test_skills_refresh_rejects_unrelated_directory(tmp_path: Path) -> None:
    marker = tmp_path / "keep.txt"
    marker.write_text("keep")
    with pytest.raises(OSError):
        prepare_codex_skills_dir(tmp_path)
    assert marker.read_text() == "keep"
