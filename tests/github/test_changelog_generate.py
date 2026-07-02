from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "changelog" / "generate.py"
spec = importlib.util.spec_from_file_location("changelog_generate", SCRIPT)
assert spec and spec.loader
gen = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = gen
spec.loader.exec_module(gen)


# --- previous_final_tag ------------------------------------------------------

_TAGS = ["v0.1.0", "v0.1.0rc4", "v0.1.1", "v0.2.0", "v0.2.0rc1", "v0.3.0", "v0.3.0rc1"]


def test_previous_final_tag_for_minor() -> None:
    assert gen.previous_final_tag("v0.3.0", _TAGS) == "v0.2.0"


def test_previous_final_tag_for_patch() -> None:
    # A patch picks the previous patch/minor, never a higher minor.
    assert gen.previous_final_tag("v0.2.1", [*_TAGS, "v0.2.1"]) == "v0.2.0"


def test_previous_final_tag_ignores_prereleases() -> None:
    assert gen.previous_final_tag("v0.2.0", _TAGS) == "v0.1.1"


def test_previous_final_tag_first_release() -> None:
    assert gen.previous_final_tag("v0.1.0", ["v0.1.0", "v0.1.0rc4"]) is None


def test_previous_final_tag_skips_rc_between_finals() -> None:
    # v0.2.0, v0.3.0rc0, v0.3.0 present → previous of v0.3.0 is v0.2.0 (rc ignored).
    assert gen.previous_final_tag("v0.3.0", ["v0.2.0", "v0.3.0rc0", "v0.3.0"]) == "v0.2.0"


# --- collect() base override -------------------------------------------------


def _stub_io(monkeypatch, *, subjects: list[str], all_tags: list[str], date: str = "2026-07-02"):
    """Stub the git/gh IO so collect() runs offline; records the range args seen."""
    seen: list[tuple[str | None, str]] = []

    def fake_range_subjects(prev, tag):
        seen.append((prev, tag))
        return subjects

    monkeypatch.setattr(gen, "_all_tags", lambda: all_tags)
    monkeypatch.setattr(gen, "_range_subjects", fake_range_subjects)
    monkeypatch.setattr(gen, "_tag_date", lambda tag: date)
    monkeypatch.setattr(gen, "_gh_pr_body", lambda repo, pr: None)
    _stub_io.seen = seen


def test_collect_uses_base_override_as_range_start(monkeypatch) -> None:
    _stub_io(monkeypatch, subjects=[], all_tags=["v0.3.0"])
    gen.collect("HEAD", "o/o", base="v0.3.0")
    # Range start is the explicit base, NOT previous_final_tag (which would raise
    # on the non-version "HEAD").
    assert _stub_io.seen == [("v0.3.0", "HEAD")]


def test_collect_without_base_uses_previous_final_tag(monkeypatch) -> None:
    _stub_io(monkeypatch, subjects=[], all_tags=["v0.2.0", "v0.3.0rc0", "v0.3.0"])
    gen.collect("v0.3.0", "o/o")
    assert _stub_io.seen == [("v0.2.0", "v0.3.0")]


# --- CLI guard rail ----------------------------------------------------------


def test_cli_non_version_tag_without_base_errors(monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["generate.py", "--tag", "preview", "--repo", "o/o"])
    with pytest.raises(SystemExit) as exc:
        gen.main()
    assert exc.value.code != 0
    assert "not a final vX.Y.Z" in capsys.readouterr().err


# --- pr_numbers_from_subjects ------------------------------------------------


def test_pr_numbers_extracted_and_deduped() -> None:
    subjects = [
        "feat(web): show progress bar (#1304)",
        "fix(policies): reject url policies (#1507)",
        "chore: no pr ref here",
        "feat(web): show progress bar (#1304)",  # duplicate
    ]
    assert gen.pr_numbers_from_subjects(subjects) == [1304, 1507]


def test_pr_titles_strip_ref_and_dedupe() -> None:
    subjects = [
        "feat(web): show progress bar (#1304)",
        "fix(policies): reject url policies (#1507)",
        "feat(web): show progress bar again (#1304)",  # dup number, first wins
    ]
    titles = gen.pr_titles_from_subjects(subjects)
    assert titles == {
        1304: "feat(web): show progress bar",
        1507: "fix(policies): reject url policies",
    }


# --- harvest_pr --------------------------------------------------------------

_TYPE_SECTION = {
    "UI / frontend change": "- [x] UI / frontend change\n- [ ] Bug fix\n",
    "Bug fix": "- [ ] UI / frontend change\n- [x] Bug fix\n",
    "Breaking change": "- [ ] Bug fix\n- [x] Breaking change\n",
    "none": "- [ ] Bug fix\n- [ ] Feature\n",
}


def _body(changelog: str | None, kind: str = "Bug fix") -> str:
    type_block = _TYPE_SECTION[kind]
    changelog_block = "" if changelog is None else f"\n## Changelog\n\n{changelog}\n"
    return f"## Summary\n\nThing.\n\n## Type of change\n\n{type_block}{changelog_block}"


def test_harvest_included_with_description_and_tag() -> None:
    body = _body("Projects workspace groups sessions", "UI / frontend change")
    result = gen.harvest_pr(123, body)
    assert result.status == "included"
    assert result.description == "Projects workspace groups sessions"
    assert result.type_tags == ["UI / frontend change"]


def test_harvest_takes_first_line_of_multiline() -> None:
    result = gen.harvest_pr(123, _body("first line of the change\n\nmore detail\nand more"))
    assert result.status == "included"
    assert result.description == "first line of the change"


def test_harvest_placeholder_is_omitted() -> None:
    placeholder = "<Add a line to describe the change, else delete this section>"
    result = gen.harvest_pr(123, _body(placeholder))
    assert result.status == "omitted"
    assert result.description == ""


def test_harvest_deleted_section_is_omitted() -> None:
    result = gen.harvest_pr(123, _body(None))
    assert result.status == "omitted"


@pytest.mark.parametrize("marker", ["skip", "n/a", "N/A", "none", "-"])
def test_harvest_omit_markers_are_omitted(marker: str) -> None:
    # Leftover `skip`/`n/a` (old template sentinel) must not leak in as an entry.
    result = gen.harvest_pr(123, _body(marker))
    assert result.status == "omitted"
    assert result.description == ""


def test_harvest_missing_body() -> None:
    assert gen.harvest_pr(123, None).status == "omitted"


# --- render_section ----------------------------------------------------------


def _result(pr: int, description: str, type_tags: list[str] | None = None) -> object:
    r = gen.HarvestResult(pr)
    r.description = description
    r.type_tags = type_tags or []
    r.status = "included" if description else "omitted"
    return r


def test_render_section_flat_list_with_tags_sorted_by_pr() -> None:
    results = [
        _result(20, "a crash fix", ["Bug fix"]),
        _result(5, "another thing", ["Feature"]),
        _result(10, "watch flag", ["UI / frontend change"]),
    ]
    section = gen.render_section("v0.3.0", "2026-06-27", results)
    assert section.startswith("## [v0.3.0] — 2026-06-27")
    # Flat list, sorted by PR number, each with its bracket tag.
    assert section.index("another thing (#5)") < section.index("watch flag (#10)")
    assert section.index("watch flag (#10)") < section.index("a crash fix (#20)")
    assert "- [UI] watch flag (#10)" in section
    assert "- [Bug fix] a crash fix (#20)" in section
    # No category sub-headings.
    assert "### " not in section


def test_render_section_multiple_tags_join() -> None:
    section = gen.render_section(
        "v0.3.0", "2026-06-27", [_result(7, "did a thing", ["UI / frontend change", "Bug fix"])]
    )
    assert "- [UI / Bug fix] did a thing (#7)" in section


def test_render_section_untagged_entry_has_no_bracket() -> None:
    section = gen.render_section("v0.3.0", "2026-06-27", [_result(7, "did a thing", [])])
    assert "- did a thing (#7)" in section


def test_render_section_omits_undocumented_prs() -> None:
    results = [_result(10, "documented", ["Bug fix"]), _result(20, "", [])]
    section = gen.render_section("v0.3.0", "2026-06-27", results)
    assert "(#10)" in section and "(#20)" not in section


def test_render_section_no_entries() -> None:
    section = gen.render_section("v0.3.0", "2026-06-27", [])
    assert "_No user-facing changes._" in section


# --- insert_section ----------------------------------------------------------

_SEED = gen._SEED_CHANGELOG


def _section(tag: str, date: str, text: str) -> str:
    return f"## [{tag}] — {date}\n\n### Added\n- {text} (#1)\n"


def test_insert_into_empty_then_order_newest_first() -> None:
    doc = gen.insert_section(_SEED, "v0.2.0", _section("v0.2.0", "2026-06-19", "two"))
    doc = gen.insert_section(doc, "v0.3.0", _section("v0.3.0", "2026-06-27", "three"))
    assert doc.index("[v0.3.0]") < doc.index("[v0.2.0]")
    # Preamble stays on top.
    assert doc.index("# Changelog") < doc.index("[v0.3.0]")


def test_insert_patch_lands_below_newer_minor() -> None:
    doc = gen.insert_section(_SEED, "v0.3.0", _section("v0.3.0", "2026-06-27", "three"))
    doc = gen.insert_section(doc, "v0.2.0", _section("v0.2.0", "2026-06-19", "two"))
    doc = gen.insert_section(doc, "v0.2.1", _section("v0.2.1", "2026-06-30", "patch"))
    assert doc.index("[v0.3.0]") < doc.index("[v0.2.1]") < doc.index("[v0.2.0]")


def test_insert_is_idempotent_and_replaces() -> None:
    doc = gen.insert_section(_SEED, "v0.3.0", _section("v0.3.0", "2026-06-27", "old"))
    doc = gen.insert_section(doc, "v0.3.0", _section("v0.3.0", "2026-06-27", "new"))
    assert doc.count("[v0.3.0]") == 1
    assert "new (#1)" in doc and "old (#1)" not in doc


# --- render_draft_notes ------------------------------------------------------

_REPO = "omnigent-ai/omnigent"


def test_draft_notes_groups_into_two_sections_by_type() -> None:
    results = [
        _result(10, "a new capability", ["Feature"]),
        _result(20, "moved a button", ["UI / frontend change"]),
        _result(30, "a crash fix", ["Bug fix"]),
        _result(40, "dropped a flag", ["Breaking change"]),
    ]
    notes = gen.render_draft_notes(results, _REPO)
    assert "## Major new features" in notes
    assert "## Bug fixes & hardening" in notes
    # Feature/UI land in features; Bug fix/Breaking in hardening.
    feat, hard = notes.split("## Bug fixes & hardening")
    assert "a new capability (#10)" in feat and "moved a button (#20)" in feat
    assert "a crash fix (#30)" in hard and "dropped a flag (#40)" in hard
    # Features section comes first.
    assert notes.index("## Major new features") < notes.index("## Bug fixes & hardening")


def test_draft_notes_has_full_changelog_footer() -> None:
    notes = gen.render_draft_notes([_result(1, "x", ["Feature"])], _REPO)
    assert notes.rstrip().endswith(
        "Full Changelog: https://github.com/omnigent-ai/omnigent/blob/main/CHANGELOG.md"
    )


def test_draft_notes_empty_section_keeps_placeholder() -> None:
    # Only a feature entry — the hardening section should still appear with a hint.
    notes = gen.render_draft_notes([_result(1, "x", ["Feature"])], _REPO)
    assert "## Bug fixes & hardening" in notes
    assert "no entries harvested" in notes


def test_draft_notes_sorted_by_pr_within_section() -> None:
    results = [
        _result(30, "third", ["Feature"]),
        _result(10, "first", ["Feature"]),
        _result(20, "second", ["UI / frontend change"]),
    ]
    notes = gen.render_draft_notes(results, _REPO)
    assert notes.index("first (#10)") < notes.index("second (#20)") < notes.index("third (#30)")


# --- render_pr_list (agent input) --------------------------------------------


def _titled(pr: int, title: str, description: str, type_tags: list[str] | None = None) -> object:
    r = gen.HarvestResult(pr, title)
    r.description = description
    r.type_tags = type_tags or []
    r.status = "included" if description else "omitted"
    return r


def test_pr_list_includes_title_and_tagged_description() -> None:
    results = [
        _titled(20, "feat(web): projects workspace", "group sessions", ["UI / frontend change"]),
        _titled(10, "chore: bump deps", ""),  # no changelog description — title only
    ]
    listing = gen.render_pr_list(results)
    # Sorted by PR number.
    assert listing.index("#10:") < listing.index("#20:")
    assert "#10: chore: bump deps" in listing
    assert "#20: feat(web): projects workspace" in listing
    assert "    - [UI] group sessions" in listing


def test_pr_list_handles_missing_title() -> None:
    listing = gen.render_pr_list([_titled(5, "", "")])
    assert "#5: (no title)" in listing
