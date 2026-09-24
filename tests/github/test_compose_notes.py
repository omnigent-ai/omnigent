from __future__ import annotations

import json
import re
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github/scripts/changelog/compose_notes.py"
compose_notes = runpy.run_path(str(SCRIPT))["compose_notes"]
REPO = "o/o"
CREDITS = [
    {"pr": pr, "author": author, "author_url": f"https://github.com/{author}"}
    for pr, author in [(1, "alice"), (2, "bob"), (3, "carol"), (4, "dave"), (5, "bob")]
]


def _raw(highlights: str) -> str:
    return f"<!-- RELEASE_NOTES -->\n{highlights}\n<!-- /RELEASE_NOTES -->"


@pytest.mark.parametrize(
    "highlights",
    [
        "## Major new features\n\n- New feature (#3, @carol)\n\n"
        + "## Breaking changes\n\n- Renamed flag (#4, @dave)\n\n"
        + "## Bug fixes\n\n- Fixed crashes (#1, @alice)",
        "## Major new features\n\n- New feature (#3, @carol)\n\n"
        + "## Bug fixes\n\n- Fixed crashes (#1, @alice)",
        "## Bug fixes\n\n- Fixed crashes (#1, @alice)",
    ],
)
def test_curated_sections_and_remaining_contributions(highlights: str) -> None:
    notes = compose_notes(_raw(highlights), CREDITS, REPO)
    highlighted = set(re.findall(r"#(\d+)", highlights))
    summary, others = notes.split("## Other contributions")
    assert re.findall(r"(?m)^## .+$", summary) == re.findall(r"(?m)^## .+$", highlights)
    assert summary.count("- ") == highlights.count("- ")
    for credit in CREDITS:
        link = f"[#{credit['pr']}](https://github.com/o/o/pull/{credit['pr']})"
        assert notes.count(link) == 1
        assert (link in summary) == (str(credit["pr"]) in highlighted)
        assert (link in others) == (str(credit["pr"]) not in highlighted)
        assert f"[@{credit['author']}]({credit['author_url']})" in notes
    assert "[#2](https://github.com/o/o/pull/2), [#5](https://github.com/o/o/pull/5), " in others
    assert "\n- " not in others
    assert "## All contributions" not in notes


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "Agent failed",
        _raw(""),
        _raw("## Highlights\n- Wrong heading (#1)"),
        _raw("## Bug fixes\n## Major new features\n- Wrong order (#1)"),
        _raw("## Bug fixes\n## Bug fixes\n- Duplicate headings (#1)"),
        _raw("## Bug fixes\n- Unknown PR (#999)"),
        _raw("## Bug fixes\n- Missing PR citation"),
    ],
)
def test_unavailable_or_invalid_highlights_keep_every_credit(raw: str) -> None:
    assert compose_notes(raw, CREDITS, REPO) == compose_notes("", CREDITS, REPO)
    notes = compose_notes(raw, CREDITS, REPO)
    assert notes.startswith("## Other contributions\n")
    assert notes.count("/pull/") == len(CREDITS)


def test_highlight_credits_use_github_authors_and_deduplicate() -> None:
    notes = compose_notes(_raw("## Bug fixes\n- Fixed issues (#2, #5, #2, @wrong)"), CREDITS, REPO)
    summary, others = notes.split("## Other contributions")
    assert "@wrong" not in notes
    assert summary.count("[@bob]") == 1
    assert summary.count("/pull/2)") == 1
    assert "/pull/2)" not in others and "/pull/5)" not in others


def test_all_highlighted_omits_empty_other_section() -> None:
    notes = compose_notes(_raw("## Bug fixes\n- Fixed (#1)"), CREDITS[:1], REPO)
    assert "Other contributions" not in notes
    assert "Full Changelog:" in notes


def test_grouping_handles_bots_deleted_authors_and_sorting() -> None:
    credits = [
        {"pr": 3, "author": "", "author_url": ""},
        {"pr": 2, "author": "bot[bot]", "author_url": "https://github.com/apps/bot"},
        {"pr": 1, "author": "bot[bot]", "author_url": "https://github.com/apps/bot"},
    ]
    notes = compose_notes("", credits, REPO)
    assert "[#3](https://github.com/o/o/pull/3), Author unavailable" in notes
    assert "Author unavailable; [#1]" in notes
    assert "[#1](https://github.com/o/o/pull/1), [#2](https://github.com/o/o/pull/2)" in notes
    assert notes.count("[@bot[bot]](https://github.com/apps/bot)") == 1


def test_cli_missing_highlights_uses_grouped_fallback(tmp_path: Path) -> None:
    credits = tmp_path / "credits.json"
    credits.write_text(json.dumps(CREDITS))
    out = tmp_path / "notes.md"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--highlights",
            str(tmp_path / "missing.txt"),
            "--credits",
            str(credits),
            "--repo",
            REPO,
            "--out",
            str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert out.read_text() == compose_notes("", CREDITS, REPO)
    assert (
        "::warning::Release highlights omitted: drafter output is empty or unavailable"
        in result.stderr
    )


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ("Agent failed", "RELEASE_NOTES markers are missing"),
        (_raw(""), "the RELEASE_NOTES block is empty"),
        (_raw("## Highlights\n- Fix (#1)"), "highlight headings"),
        (_raw("## Bug fixes\n- Fixed (#1)\n  wrapped text"), "highlight line 3"),
        (
            _raw("## Bug fixes\n- Fixed (#1) with more text"),
            "must be a bullet ending in PR citations",
        ),
        (_raw("## Bug fixes\n- Fixed (#999)"), "cites a PR outside the release"),
        (_raw("## Bug fixes"), "contains no highlight bullets"),
    ],
)
def test_fallback_warning_explains_reason_and_preserves_credits(raw, reason, capsys) -> None:
    notes = compose_notes(raw, CREDITS, REPO, warn_on_fallback=True)
    assert notes == compose_notes("", CREDITS, REPO)
    warning = capsys.readouterr().err
    assert "::warning::Release highlights omitted:" in warning
    assert reason in warning
    assert "Keeping complete contributor groups" in warning


def test_valid_highlights_and_intentional_fallback_do_not_warn(capsys) -> None:
    compose_notes(_raw("## Bug fixes\n- Fixed (#1)"), CREDITS, REPO, warn_on_fallback=True)
    compose_notes("", CREDITS, REPO)
    assert capsys.readouterr().err == ""


def test_repeated_pr_across_highlights_falls_back_with_reason(capsys) -> None:
    raw = _raw("## Major new features\n- Feature (#1)\n## Bug fixes\n- Fix (#1)")
    assert compose_notes(raw, CREDITS, REPO, warn_on_fallback=True) == compose_notes(
        "", CREDITS, REPO
    )
    assert "repeats an already-cited PR" in capsys.readouterr().err


def test_cli_invalid_utf8_preserves_complete_fallback(tmp_path: Path) -> None:
    credits = tmp_path / "credits.json"
    credits.write_text(json.dumps(CREDITS))
    highlights = tmp_path / "highlights.txt"
    highlights.write_bytes(b"\xff")
    out = tmp_path / "notes.md"
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--highlights",
            str(highlights),
            "--credits",
            str(credits),
            "--repo",
            REPO,
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "RELEASE_NOTES markers are missing" in result.stderr
    assert out.read_text() == compose_notes("", CREDITS, REPO)
