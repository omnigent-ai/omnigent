"""Tests for the codex home staging root and its sandbox-exposure globber.

The wrapped codex executor stages per-conversation CODEX_HOMEs under a
well-known temp-dir root so sandbox backends can re-expose exactly the
``skills/`` subtree of each home (and nothing else — ``auth.json`` &
co stay hidden). These tests pin the root's location/permissions and
the globber's selection + fail-closed safety semantics.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path

import pytest

from omnigent.inner.codex_staging import (
    CODEX_HOME_PREFIX,
    codex_home_staging_root,
    staged_codex_skill_dirs,
)


@pytest.fixture
def isolated_tempdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point ``tempfile.gettempdir()`` at a per-test dir.

    The staging helpers derive every path from ``tempfile.gettempdir()``
    at call time, so this isolates them from the host's real temp dir
    (which may carry live staged homes from concurrent sessions).
    """
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    return tmp_path


def _stage_home(root: Path, name_suffix: str, *, with_skills: bool = True) -> Path:
    home = root / f"{CODEX_HOME_PREFIX}{name_suffix}"
    home.mkdir()
    (home / "auth.json").write_text('{"secret": "never-expose"}')
    if with_skills:
        skill = home / "skills" / "demo-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("body\n")
    return home


def test_staging_root_is_private_and_under_tempdir(isolated_tempdir: Path) -> None:
    """The root is created inside the system temp dir, private to the user.

    ``0o700``: the temp dir is shared, and a group/other-writable root
    would let another principal plant content that the globber then
    mounts into sandboxes.
    """
    root = codex_home_staging_root()
    assert root.parent == isolated_tempdir
    assert root.is_dir()
    if hasattr(os, "getuid"):
        assert f"-{os.getuid()}" in root.name
        assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_staging_root_tightens_a_loose_preexisting_mode(isolated_tempdir: Path) -> None:
    """A pre-existing root with a permissive mode is chmodded back to 0700,
    so the globber's fail-closed check doesn't silently disable skill
    exposure forever after one bad umask.
    """
    root = codex_home_staging_root()
    root.chmod(0o770)
    assert stat.S_IMODE(codex_home_staging_root().stat().st_mode) == 0o700


def test_globber_returns_only_skills_subtrees_of_staged_homes(
    isolated_tempdir: Path,
) -> None:
    """Only ``<home>/skills`` dirs come back — never the home itself (whose
    siblings hold credentials), never non-prefixed entries, never homes
    without a skills dir.
    """
    root = codex_home_staging_root()
    with_skills = _stage_home(root, "aaa")
    _stage_home(root, "bbb", with_skills=False)
    stranger = root / "unrelated-dir"
    stranger.mkdir()
    (stranger / "skills").mkdir()

    dirs = staged_codex_skill_dirs()

    assert dirs == [with_skills / "skills"]


def test_globber_is_empty_when_root_is_absent(isolated_tempdir: Path) -> None:
    """No staging root (no codex session ever staged) → no grants."""
    assert staged_codex_skill_dirs() == []


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX permission semantics")
def test_globber_fails_closed_on_a_group_or_other_writable_root(
    isolated_tempdir: Path,
) -> None:
    """A root someone loosened must contribute nothing to sandbox mounts:
    group/other write access would let another principal swap mount
    sources between glob and spawn.
    """
    root = codex_home_staging_root()
    _stage_home(root, "aaa")
    assert staged_codex_skill_dirs() != []

    root.chmod(0o707)
    assert staged_codex_skill_dirs() == []
