"""Private home and session-owned skill staging safety."""

from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.inner import codex_staging
from omnigent.inner.codex_staging import (
    CODEX_SKILLS_PREFIX,
    _staging_root_path,
    codex_home_staging_root,
    link_codex_skills_dir,
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


def _refuse_symlink(*_args: object, **_kwargs: object) -> None:
    # What Windows raises without Developer Mode or the symlink privilege.
    raise OSError(1314, "A required privilege is not held by the client")


def _skills_link_paths(root: Path) -> tuple[Path, Path]:
    skills_dir = root / f"{CODEX_SKILLS_PREFIX}session"
    skills_dir.mkdir(mode=0o700)
    home = root / "home"
    home.mkdir()
    return home / "skills", skills_dir


def test_skills_link_falls_back_to_a_junction_when_symlinks_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_skills, skills_dir = _skills_link_paths(tmp_path)
    junctions: list[tuple[str, str]] = []
    monkeypatch.setattr(Path, "symlink_to", _refuse_symlink)
    monkeypatch.setattr(codex_staging, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setitem(
        sys.modules,
        "_winapi",
        SimpleNamespace(CreateJunction=lambda target, link: junctions.append((target, link))),
    )

    link_codex_skills_dir(home_skills, skills_dir)

    assert junctions == [(str(skills_dir), str(home_skills))]


def test_skills_link_raises_off_windows_when_symlinks_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_skills, skills_dir = _skills_link_paths(tmp_path)
    monkeypatch.setattr(Path, "symlink_to", _refuse_symlink)
    monkeypatch.setattr(codex_staging, "sys", SimpleNamespace(platform="linux"))

    with pytest.raises(OSError, match="privilege"):
        link_codex_skills_dir(home_skills, skills_dir)
    assert not home_skills.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows-only")
def test_skills_link_junction_resolves_into_the_granted_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home_skills, skills_dir = _skills_link_paths(tmp_path)
    (skills_dir / "SKILL.md").write_text("body")
    monkeypatch.setattr(Path, "symlink_to", _refuse_symlink)

    link_codex_skills_dir(home_skills, skills_dir)

    assert home_skills.is_junction()
    assert (home_skills / "SKILL.md").resolve() == (skills_dir / "SKILL.md").resolve()


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows-only")
@pytest.mark.parametrize("relative_path", ["linked", "nested/linked"])
def test_skills_refresh_does_not_follow_junctions(tmp_path: Path, relative_path: str) -> None:
    """Refreshing the grant removes junctions without deleting their outside targets."""
    import _winapi

    _, skills_dir = _skills_link_paths(tmp_path)
    identity = skills_dir.stat().st_ino
    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep")
    junction = skills_dir / relative_path
    junction.parent.mkdir(parents=True, exist_ok=True)
    _winapi.CreateJunction(str(outside), str(junction))

    prepare_codex_skills_dir(skills_dir)

    assert marker.read_text() == "keep"
    assert skills_dir.stat().st_ino == identity
    assert list(skills_dir.iterdir()) == []


@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions are Windows-only")
def test_skills_refresh_rejects_junction_root(tmp_path: Path) -> None:
    """A junction cannot stand in for the private directory owned by this session."""
    import _winapi

    outside = tmp_path / "outside"
    outside.mkdir()
    marker = outside / "keep.txt"
    marker.write_text("keep")
    root = tmp_path / f"{CODEX_SKILLS_PREFIX}session"
    _winapi.CreateJunction(str(outside), str(root))

    with pytest.raises(OSError):
        prepare_codex_skills_dir(root)

    assert marker.read_text() == "keep"
