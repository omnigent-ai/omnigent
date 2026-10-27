"""Installed skills become bundle skills without disturbing the bundle's own."""

from __future__ import annotations

import io
import tarfile

import pytest

from omnigent.skills.overlay import (
    SkillTarballError,
    bundle_skill_names,
    overlay_skills,
    validate_skill_tarball,
)


def _tar(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return out.getvalue()


def _names(data: bytes) -> list[str]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        return sorted(tf.getnames())


BUNDLE = _tar({"config.yaml": b"name: a\n", "skills/own/SKILL.md": b"---\nname: own\n---\n"})
SKILL = _tar({"SKILL.md": b"---\nname: review\n---\n", "notes.txt": b"n"})


def test_overlay_adds_skills_under_the_bundle_skills_dir() -> None:
    merged = overlay_skills(BUNDLE, [("review", SKILL)])
    assert _names(merged) == [
        "config.yaml",
        "skills/own/SKILL.md",
        "skills/review/SKILL.md",
        "skills/review/notes.txt",
    ]
    with tarfile.open(fileobj=io.BytesIO(merged), mode="r:gz") as tf:
        assert tf.extractfile("skills/review/notes.txt").read() == b"n"


def test_bundle_skills_win_a_name_collision() -> None:
    merged = overlay_skills(BUNDLE, [("own", SKILL)])
    assert _names(merged) == _names(BUNDLE)
    assert bundle_skill_names(merged) == {"own"}


def test_no_skills_returns_the_bundle_untouched() -> None:
    assert overlay_skills(BUNDLE, []) is BUNDLE


@pytest.mark.parametrize(
    ("files", "reason"),
    [
        ({"notes.txt": b"n"}, "no SKILL.md"),
        ({"../SKILL.md": b"x"}, "unsafe path"),
        ({"/SKILL.md": b"x"}, "unsafe path"),
    ],
)
def test_unsafe_or_incomplete_skill_tarballs_are_rejected(
    files: dict[str, bytes], reason: str
) -> None:
    with pytest.raises(SkillTarballError, match=reason):
        validate_skill_tarball(_tar(files))


def test_a_link_in_a_skill_tarball_is_rejected() -> None:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tf:
        info = tarfile.TarInfo("SKILL.md")
        info.size = 0
        tf.addfile(info, io.BytesIO(b""))
        link = tarfile.TarInfo("evil")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        tf.addfile(link)
    with pytest.raises(SkillTarballError, match="unsupported entry type"):
        overlay_skills(BUNDLE, [("evil", out.getvalue())])
