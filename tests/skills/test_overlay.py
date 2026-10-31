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


def test_namespaced_and_reserved_names_keep_bundle_precedence() -> None:
    # A bundle skill two levels down is still ``triage``; the parsed spec's
    # canonical names (``code-review`` for a legacy ``Code_Review`` dir) win too.
    bundle = _tar(
        {
            "config.yaml": b"name: a\n",
            "skills/team/triage/SKILL.md": b"---\nname: triage\n---\n",
            "skills/Code_Review/SKILL.md": b"---\nname: review\n---\n",
        }
    )
    assert bundle_skill_names(bundle) == {"triage", "Code_Review"}
    merged = overlay_skills(
        bundle,
        [("triage", SKILL), ("code-review", SKILL), ("fresh", SKILL)],
        reserved={"code-review"},
    )
    assert _names(merged) == [
        "config.yaml",
        "skills/Code_Review/SKILL.md",
        "skills/fresh/SKILL.md",
        "skills/fresh/notes.txt",
        "skills/team/triage/SKILL.md",
    ]


def test_overlay_bytes_do_not_depend_on_the_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    # The runner caches by the hash of the response, so the same inputs must
    # give the same bytes whenever they are built.
    import time

    monkeypatch.setattr(time, "time", lambda: 1_000.0)
    first = overlay_skills(BUNDLE, [("review", SKILL)])
    monkeypatch.setattr(time, "time", lambda: 2_000.0)
    second = overlay_skills(BUNDLE, [("review", SKILL)])
    assert first == second
    assert _names(first) == _names(second) and "skills/review/SKILL.md" in _names(first)


def test_overlay_targets_a_child_agents_directory() -> None:
    # A session running a child reads skills from agents/<dir>/skills, not the root.
    bundle = _tar(
        {
            "config.yaml": b"name: a\n",
            "agents/child/config.yaml": b"name: child\n",
            "agents/child/skills/own/SKILL.md": b"---\nname: own\n---\n",
        }
    )
    merged = overlay_skills(bundle, [("review", SKILL), ("own", SKILL)], prefix="agents/child/")
    assert _names(merged) == [
        "agents/child/config.yaml",
        "agents/child/skills/own/SKILL.md",
        "agents/child/skills/review/SKILL.md",
        "agents/child/skills/review/notes.txt",
        "config.yaml",
    ]
    assert bundle_skill_names(merged, prefix="agents/child/") == {"own", "review"}
    assert bundle_skill_names(merged) == set()


def test_only_the_root_skill_md_is_the_manifest() -> None:
    from omnigent.skills.overlay import is_root_manifest

    assert is_root_manifest("SKILL.md") and is_root_manifest("./SKILL.md")
    assert not is_root_manifest(".SKILL.md") and not is_root_manifest("docs/SKILL.md")
    with pytest.raises(SkillTarballError, match=r"no SKILL\.md at its root"):
        validate_skill_tarball(_tar({".SKILL.md": b"decoy"}))
