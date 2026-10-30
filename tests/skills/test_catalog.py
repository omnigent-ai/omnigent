"""Indexing a checkout: plain and marketplace layouts, precedence, and what is skipped."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from omnigent.skills.catalog import index_checkout


def test_plain_repo_lists_root_and_namespaced_skills(plain_repo: Path) -> None:
    entries = index_checkout(plain_repo)
    assert [(e.name, e.description, e.display_name, e.path, e.plugin) for e in entries] == [
        ("review", "Review a diff", "Code Review", "skills/review", None),
        ("triage", "Triage issues", None, "skills/team/triage", None),
    ]


def test_marketplace_indexes_in_repo_plugins_and_reports_the_rest(marketplace_repo: Path) -> None:
    skipped: list[str] = []
    entries = index_checkout(marketplace_repo, skipped=skipped)
    # The root skill shadows the plugin's same-named skill.
    assert [(e.name, e.description, e.plugin) for e in entries] == [
        ("changelog", "Root changelog", None),
    ]
    assert any("'remote'" in note and "another repository" in note for note in skipped)
    assert any("'escape'" in note and "not a directory" in note for note in skipped)

    # Without the shadowing root skill, the plugin's own skill is listed under it.
    shutil.rmtree(marketplace_repo / "skills")
    assert [(e.name, e.plugin, e.path) for e in index_checkout(marketplace_repo)] == [
        ("changelog", "docs", "plugins/docs/skills/changelog"),
    ]


def test_empty_or_missing_layouts_index_nothing(tmp_path: Path) -> None:
    assert index_checkout(tmp_path) == []


def test_a_catalog_stops_reading_at_its_entry_cap(
    plain_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The cap bounds the manifests opened, not just the entries returned: a
    # repository of ten thousand skills costs the same as one of the cap.
    from omnigent.skills import catalog
    from tests._helpers.skill_repos import write_skill

    write_skill(plain_repo / "skills", "zzz", "Never read")
    parsed: list[str] = []
    real_parse = catalog._parse_skill
    monkeypatch.setattr(
        catalog,
        "_parse_skill",
        lambda manifest: (parsed.append(manifest.parent.name), real_parse(manifest))[1],
    )
    monkeypatch.setattr(catalog, "MAX_CATALOG_ENTRIES", 1)
    skipped: list[str] = []
    assert [e.name for e in index_checkout(plain_repo, skipped=skipped)] == ["review"]
    assert parsed == ["review"]
    assert "catalog truncated at 1 skills" in skipped


def test_links_are_never_followed(plain_repo: Path, tmp_path: Path) -> None:
    # A skill whose SKILL.md is a link to a file outside the checkout, a skill
    # directory that is a link, and a linked skills root: all skipped unread.
    outside = tmp_path / "outside.md"
    outside.write_text("---\nname: leaked\ndescription: secret\n---\n")
    linked_md = plain_repo / "skills" / "leak"
    linked_md.mkdir()
    (linked_md / "SKILL.md").symlink_to(outside)
    (plain_repo / "skills" / "linkdir").symlink_to(tmp_path)
    (plain_repo / ".agents").mkdir()
    (plain_repo / ".agents" / "skills").symlink_to(plain_repo / "skills")

    skipped: list[str] = []
    entries = index_checkout(plain_repo, skipped=skipped)

    assert [e.name for e in entries] == ["review", "triage"]
    assert not any(e.description == "secret" for e in entries)
    assert any("skills/leak: SKILL.md is a link" in note for note in skipped)
    assert any("skills directory is a link" in note for note in skipped)


def test_an_internal_skill_keeps_its_user_invocable_flag(plain_repo: Path) -> None:
    # The flag has to survive indexing, or an installed orchestration skill
    # would be advertised in the menu the way a bundled one never is.
    (plain_repo / "skills" / "orchestrate").mkdir()
    (plain_repo / "skills" / "orchestrate" / "SKILL.md").write_text(
        "---\nname: orchestrate\ndescription: internal\nuser-invocable: false\n---\nDo it.\n"
    )

    entries = {entry.name: entry for entry in index_checkout(plain_repo)}

    assert entries["orchestrate"].user_invocable is False
    assert entries["review"].user_invocable is True


def test_frontmatter_that_would_expand_or_stringify_a_structure_is_skipped(
    plain_repo: Path,
) -> None:
    # A repository is untrusted input. Aliases expand as the document loads, so
    # a few hundred bytes can build an enormous structure under a byte limit
    # that only bounds the file; and str() of a structure is far larger than
    # the YAML that produced it. Both are refused instead, which this pins with
    # small payloads rather than a resource-hungry one.
    (plain_repo / "skills" / "aliased").mkdir()
    (plain_repo / "skills" / "aliased" / "SKILL.md").write_text(
        "---\nname: aliased\nseed: &s [a, a]\ndescription: [*s, *s]\n---\nBoom.\n"
    )
    (plain_repo / "skills" / "listy").mkdir()
    (plain_repo / "skills" / "listy" / "SKILL.md").write_text(
        "---\nname: listy\ndescription:\n  - one\n  - two\n---\nNo.\n"
    )

    skipped: list[str] = []
    names = [entry.name for entry in index_checkout(plain_repo, skipped=skipped)]

    assert "aliased" not in names and "listy" not in names
    assert any("skills/aliased" in line for line in skipped), skipped
    assert any("skills/listy" in line and "must be a single value" in line for line in skipped)
    # The well-formed skills in the same repository are still indexed.
    assert "review" in names


def test_oversized_or_multibyte_frontmatter_is_skipped(plain_repo: Path) -> None:
    from tests.skills.conftest import write_skill

    write_skill(plain_repo / "skills", "wordy", "é" * 1025)
    write_skill(plain_repo / "skills", "titled", "fine", label="名" * 257)
    (plain_repo / "skills" / "huge").mkdir()
    (plain_repo / "skills" / "huge" / "SKILL.md").write_bytes(
        b"---\nname: huge\ndescription: d\n---\n" + b"x" * (1024 * 1024 + 1)
    )

    skipped: list[str] = []
    names = [e.name for e in index_checkout(plain_repo, skipped=skipped)]

    assert "wordy" not in names and "titled" not in names and "huge" not in names
    assert any("skills/wordy: description or path is too long" in n for n in skipped)
    assert any("skills/titled: display name is too long" in n for n in skipped)
    assert any(
        "skills/huge: SKILL.md is missing, not a regular file, or too large" in n for n in skipped
    )


def test_a_name_the_runner_would_refuse_is_skipped(tmp_path: Path) -> None:
    from tests._helpers.skill_repos import write_skill

    # The runner validates bundled skill names as [a-z0-9-]{1,64}; one name it
    # refuses fails the whole spec, so every session of that user would break.
    repo = tmp_path / "repo"
    write_skill(repo / "skills", "pdf-tools", "PDF tools")
    write_skill(repo / "skills", "pdf_tools", "Underscored")
    write_skill(repo / "skills", "Review", "Capitalised")
    write_skill(repo / "skills", "a" * 65, "Too long")
    skipped: list[str] = []
    assert [e.name for e in index_checkout(repo, skipped=skipped)] == ["pdf-tools"]
    assert len([note for note in skipped if "not a valid skill name" in note]) == 3


def test_a_directory_is_indexed_as_its_own_catalog(tmp_path: Path) -> None:
    from tests._helpers.skill_repos import write_skill

    repo = tmp_path / "mono"
    write_skill(repo / "team-a" / "skills", "lint", "Lint")
    write_skill(repo / "team-b" / "skills", "deploy", "Deploy")
    # Entry paths stay relative to the repository root, so packaging finds them.
    entries = index_checkout(repo / "team-a", prefix="team-a")
    assert [(e.name, e.path) for e in entries] == [("lint", "team-a/skills/lint")]
    # A link to one skill's directory is that skill, not the catalog around it.
    single = index_checkout(repo / "team-a" / "skills" / "lint", prefix="team-a/skills/lint")
    assert [(e.name, e.path) for e in single] == [("lint", "team-a/skills/lint")]
