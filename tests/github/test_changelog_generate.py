from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

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


def _body(changelog: str) -> str:
    return f"## Summary\n\nThing.\n\n## Changelog\n\n{changelog}\n"


def test_harvest_includes_entries() -> None:
    result = gen.harvest_pr(123, _body("Added: a new flag\nFixed: a crash"))
    assert result.status == "included"
    assert result.entries == [("Added", "a new flag"), ("Fixed", "a crash")]


def test_harvest_skip() -> None:
    result = gen.harvest_pr(123, _body("skip"))
    assert result.status == "skip"
    assert result.entries == []


def test_harvest_no_section() -> None:
    result = gen.harvest_pr(123, "## Summary\n\nNo changelog heading here.\n")
    assert result.status == "no-section"


def test_harvest_missing_body() -> None:
    assert gen.harvest_pr(123, None).status == "no-section"


def test_harvest_unparseable() -> None:
    result = gen.harvest_pr(123, _body("just prose, no category"))
    assert result.status == "unparseable"
    assert result.entries == []


# --- render_section ----------------------------------------------------------


def _result(pr: int, entries: list[tuple[str, str]]) -> object:
    r = gen.HarvestResult(pr)
    r.entries = entries
    r.status = "included"
    return r


def test_render_section_groups_by_category() -> None:
    results = [
        _result(10, [("Added", "watch flag")]),
        _result(20, [("Fixed", "a crash")]),
        _result(5, [("Added", "another thing")]),
    ]
    section = gen.render_section("v0.3.0", "2026-06-27", results)
    assert section.startswith("## [v0.3.0] — 2026-06-27")
    assert "### Added" in section and "### Fixed" in section
    # Sorted by PR number within a category.
    assert section.index("another thing (#5)") < section.index("watch flag (#10)")
    # Category order follows CHANGELOG_CATEGORIES (Added before Fixed).
    assert section.index("### Added") < section.index("### Fixed")


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


def test_draft_notes_groups_into_two_sections() -> None:
    results = [
        _result(10, [("Added", "a new flag")]),
        _result(20, [("Changed", "moved a button")]),
        _result(30, [("Fixed", "a crash")]),
        _result(40, [("Security", "patched an SSRF")]),
    ]
    notes = gen.render_draft_notes(results, _REPO)
    assert "## Major new features" in notes
    assert "## Bug fixes & hardening" in notes
    # Added/Changed land in features; Fixed/Security in hardening.
    feat, hard = notes.split("## Bug fixes & hardening")
    assert "a new flag (#10)" in feat and "moved a button (#20)" in feat
    assert "a crash (#30)" in hard and "patched an SSRF (#40)" in hard
    # Features section comes first.
    assert notes.index("## Major new features") < notes.index("## Bug fixes & hardening")


def test_draft_notes_has_full_changelog_footer() -> None:
    notes = gen.render_draft_notes([_result(1, [("Added", "x")])], _REPO)
    assert notes.rstrip().endswith(
        "Full Changelog: https://github.com/omnigent-ai/omnigent/blob/main/CHANGELOG.md"
    )


def test_draft_notes_empty_section_keeps_placeholder() -> None:
    # Only a feature entry — the hardening section should still appear with a hint.
    notes = gen.render_draft_notes([_result(1, [("Added", "x")])], _REPO)
    assert "## Bug fixes & hardening" in notes
    assert "no entries harvested" in notes


def test_draft_notes_sorted_by_pr_within_section() -> None:
    results = [
        _result(30, [("Added", "third")]),
        _result(10, [("Added", "first")]),
        _result(20, [("Changed", "second")]),
    ]
    notes = gen.render_draft_notes(results, _REPO)
    assert notes.index("first (#10)") < notes.index("second (#20)") < notes.index("third (#30)")


# --- render_pr_list (agent input) --------------------------------------------


def _titled(pr: int, title: str, entries: list[tuple[str, str]]) -> object:
    r = gen.HarvestResult(pr, title)
    r.entries = entries
    r.status = "included" if entries else "no-section"
    return r


def test_pr_list_includes_title_and_entries() -> None:
    results = [
        _titled(20, "feat(web): projects workspace", [("Added", "group sessions")]),
        _titled(10, "chore: bump deps", []),  # no changelog entry — title only
    ]
    listing = gen.render_pr_list(results)
    # Sorted by PR number.
    assert listing.index("#10:") < listing.index("#20:")
    assert "#10: chore: bump deps" in listing
    assert "#20: feat(web): projects workspace" in listing
    assert "    - [Added] group sessions" in listing


def test_pr_list_handles_missing_title() -> None:
    listing = gen.render_pr_list([_titled(5, "", [])])
    assert "#5: (no title)" in listing
