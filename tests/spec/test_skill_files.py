"""Safety + behavior tests for skill resource-tree browsing helpers."""

from pathlib import Path

import pytest

from omnigent.spec.skill_files import (
    MAX_SKILL_FILE_BYTES,
    SkillFileError,
    list_skill_tree,
    read_skill_file,
    resolve_skill_file,
)


def _make_skill(tmp_path: Path) -> Path:
    """A skill dir with a nested references/scripts/assets layout."""
    skill = tmp_path / "skill"
    (skill / "references").mkdir(parents=True)
    (skill / "scripts").mkdir()
    (skill / "assets").mkdir()
    (skill / "SKILL.md").write_text("# skill\n\nbody")
    (skill / "references" / "guide.md").write_text("## guide")
    (skill / "references" / "deep").mkdir()
    (skill / "references" / "deep" / "note.txt").write_text("deep note")
    (skill / "scripts" / "run.sh").write_text("#!/bin/sh\necho hi\n")
    (skill / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\x01\x02\x03")
    return skill


def test_tree_lists_nested_files_with_dirs_first(tmp_path: Path) -> None:
    skill = _make_skill(tmp_path)
    nodes = list_skill_tree(skill)
    paths = [(n.kind, n.path) for n in nodes]

    # Every file + directory is present, addressed relative to the skill root.
    assert ("dir", "references") in paths
    assert ("file", "references/guide.md") in paths
    assert ("dir", "references/deep") in paths
    assert ("file", "references/deep/note.txt") in paths
    assert ("file", "scripts/run.sh") in paths
    assert ("file", "assets/logo.png") in paths
    assert ("file", "SKILL.md") in paths

    # A directory node precedes its own children (parents before descendants).
    assert paths.index(("dir", "references")) < paths.index(("file", "references/guide.md"))
    # Sizes: files carry a byte size, directories don't.
    by_path = {n.path: n for n in nodes}
    assert by_path["SKILL.md"].size == len("# skill\n\nbody")
    assert by_path["references"].size is None


def test_top_level_orders_dirs_before_files_alphabetically(tmp_path: Path) -> None:
    skill = _make_skill(tmp_path)
    top = [n.path for n in list_skill_tree(skill) if "/" not in n.path]
    # assets, references, scripts (dirs, alpha) come before SKILL.md (file).
    assert top == ["assets", "references", "scripts", "SKILL.md"]


def test_empty_tree_is_not_an_error(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    assert list_skill_tree(empty) == []
    # A missing directory is also just an empty tree, never a raise.
    assert list_skill_tree(tmp_path / "does-not-exist") == []


def test_read_text_file_returns_utf8_content(tmp_path: Path) -> None:
    skill = _make_skill(tmp_path)
    content = read_skill_file(skill, "references/guide.md")
    assert content.is_text is True
    assert content.too_large is False
    assert content.text == "## guide"
    assert content.path == "references/guide.md"


def test_read_binary_file_is_metadata_only(tmp_path: Path) -> None:
    skill = _make_skill(tmp_path)
    content = read_skill_file(skill, "assets/logo.png")
    assert content.is_text is False
    assert content.too_large is False
    assert content.text is None
    assert content.size > 0


def test_read_oversized_file_is_download_only(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    big = skill / "big.txt"
    big.write_bytes(b"a" * (MAX_SKILL_FILE_BYTES + 1))
    content = read_skill_file(skill, "big.txt")
    assert content.too_large is True
    assert content.is_text is False
    assert content.text is None
    assert content.size == MAX_SKILL_FILE_BYTES + 1


@pytest.mark.parametrize(
    "bad_path",
    ["../secret.txt", "../../etc/passwd", "references/../../escape.txt", "a/../../b"],
)
def test_traversal_is_rejected(tmp_path: Path, bad_path: str) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    (tmp_path / "secret.txt").write_text("secret")
    with pytest.raises(SkillFileError) as excinfo:
        resolve_skill_file(skill, bad_path)
    assert excinfo.value.status == 400


def test_absolute_and_empty_paths_are_rejected(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    with pytest.raises(SkillFileError) as abs_exc:
        resolve_skill_file(skill, "/etc/passwd")
    assert abs_exc.value.status == 400
    with pytest.raises(SkillFileError) as empty_exc:
        resolve_skill_file(skill, "")
    assert empty_exc.value.status == 400


def test_symlink_escape_is_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret")
    skill = tmp_path / "skill"
    skill.mkdir()
    # A symlinked FILE pointing outside the skill dir.
    (skill / "link.txt").symlink_to(outside / "secret.txt")
    # A symlinked DIR used as a traversal stepping stone.
    (skill / "escape").symlink_to(outside)

    with pytest.raises(SkillFileError) as file_exc:
        resolve_skill_file(skill, "link.txt")
    assert file_exc.value.status == 400
    with pytest.raises(SkillFileError) as dir_exc:
        resolve_skill_file(skill, "escape/secret.txt")
    assert dir_exc.value.status == 400


def test_symlinks_are_omitted_from_the_tree(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret")
    skill = tmp_path / "skill"
    skill.mkdir()
    (skill / "real.md").write_text("real")
    (skill / "link.txt").symlink_to(outside / "secret.txt")
    (skill / "escape").symlink_to(outside)

    paths = {n.path for n in list_skill_tree(skill)}
    assert "real.md" in paths
    # Neither the symlinked file nor the symlinked dir (nor its contents) leak.
    assert "link.txt" not in paths
    assert "escape" not in paths
    assert not any(p.startswith("escape/") for p in paths)


def test_missing_file_is_404(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    skill.mkdir()
    with pytest.raises(SkillFileError) as excinfo:
        resolve_skill_file(skill, "references/nope.md")
    assert excinfo.value.status == 404


def test_reading_a_directory_is_404(tmp_path: Path) -> None:
    skill = tmp_path / "skill"
    (skill / "references").mkdir(parents=True)
    with pytest.raises(SkillFileError) as excinfo:
        resolve_skill_file(skill, "references")
    assert excinfo.value.status == 404
