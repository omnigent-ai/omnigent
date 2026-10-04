from __future__ import annotations

import importlib.util
import json
import re
import subprocess
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


def test_cli_non_version_ref_without_base_errors(monkeypatch, capsys) -> None:
    # A non-version ref (branch/sha) can't be ordered and has no default range.
    monkeypatch.setattr(sys, "argv", ["generate.py", "--tag", "ci-test", "--repo", "o/o"])
    with pytest.raises(SystemExit) as exc:
        gen.main()
    assert exc.value.code != 0
    assert "not a PEP 440 version" in capsys.readouterr().err


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
    section = gen.render_section("v0.3.0", "2026-06-27", results, _REPO)
    assert section.startswith("## [v0.3.0] — 2026-06-27")
    # Flat list, sorted by PR number, each with its bracket tag.
    assert section.index(
        "another thing ([#5](https://github.com/omnigent-ai/omnigent/pull/5))"
    ) < section.index("watch flag ([#10](https://github.com/omnigent-ai/omnigent/pull/10))")
    assert section.index(
        "watch flag ([#10](https://github.com/omnigent-ai/omnigent/pull/10))"
    ) < section.index("a crash fix ([#20](https://github.com/omnigent-ai/omnigent/pull/20))")
    assert "- [UI] watch flag ([#10](https://github.com/omnigent-ai/omnigent/pull/10))" in section
    assert (
        "- [Bug fix] a crash fix ([#20](https://github.com/omnigent-ai/omnigent/pull/20))"
        in section
    )
    # No category sub-headings.
    assert "### " not in section


def test_render_section_multiple_tags_join() -> None:
    section = gen.render_section(
        "v0.3.0",
        "2026-06-27",
        [_result(7, "did a thing", ["UI / frontend change", "Bug fix"])],
        _REPO,
    )
    assert (
        "- [UI / Bug fix] did a thing ([#7](https://github.com/omnigent-ai/omnigent/pull/7))"
        in section
    )


def test_render_section_untagged_entry_has_no_bracket() -> None:
    section = gen.render_section("v0.3.0", "2026-06-27", [_result(7, "did a thing", [])], _REPO)
    assert "- did a thing ([#7](https://github.com/omnigent-ai/omnigent/pull/7))" in section


def test_render_section_includes_undocumented_prs() -> None:
    results = [_result(10, "documented", ["Bug fix"]), _result(20, "", [])]
    section = gen.render_section("v0.3.0", "2026-06-27", results, _REPO)
    assert (
        "([#10](https://github.com/omnigent-ai/omnigent/pull/10))" in section
        and "([#20](https://github.com/omnigent-ai/omnigent/pull/20))" in section
    )


def test_render_section_no_entries() -> None:
    section = gen.render_section("v0.3.0", "2026-06-27", [], _REPO)
    assert "_No pull requests in this release._" in section


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


def test_insert_prerelease_orders_by_pep440_and_coexists() -> None:
    # A dev tag lands above the previous final; the eventual final sorts above the
    # dev; an rc sits between them. All three (final/rc/dev) coexist as blocks.
    doc = gen.insert_section(_SEED, "v0.3.0", _section("v0.3.0", "2026-06-26", "three"))
    doc = gen.insert_section(doc, "v0.4.0dev0", _section("v0.4.0dev0", "2026-07-02", "dev"))
    doc = gen.insert_section(doc, "v0.4.0", _section("v0.4.0", "2026-07-10", "final"))
    doc = gen.insert_section(doc, "v0.4.0rc1", _section("v0.4.0rc1", "2026-07-08", "rc"))
    headers = re.findall(r"(?m)^## \[([^\]]+)\]", doc)
    assert headers == ["v0.4.0", "v0.4.0rc1", "v0.4.0dev0", "v0.3.0"]


def test_insert_final_and_dev_are_distinct_blocks() -> None:
    # v0.4.0 must NOT overwrite v0.4.0dev0 (no collapse of dev → final).
    doc = gen.insert_section(_SEED, "v0.4.0dev0", _section("v0.4.0dev0", "2026-07-02", "dev"))
    doc = gen.insert_section(doc, "v0.4.0", _section("v0.4.0", "2026-07-10", "final"))
    assert doc.count("[v0.4.0dev0]") == 1
    assert re.search(r"(?m)^## \[v0\.4\.0\]", doc) is not None
    assert "dev (#1)" in doc and "final (#1)" in doc


def test_insert_dev_tag_idempotent() -> None:
    doc = gen.insert_section(_SEED, "v0.4.0dev0", _section("v0.4.0dev0", "2026-07-02", "old"))
    doc = gen.insert_section(doc, "v0.4.0dev0", _section("v0.4.0dev0", "2026-07-02", "new"))
    assert doc.count("[v0.4.0dev0]") == 1
    assert "new (#1)" in doc and "old (#1)" not in doc


# --- render_draft_notes ------------------------------------------------------

_REPO = "omnigent-ai/omnigent"


def test_draft_notes_has_full_changelog_footer() -> None:
    notes = gen.render_draft_notes([_result(1, "x", ["Feature"])], _REPO)
    assert notes.rstrip().endswith(
        "Full Changelog: https://github.com/omnigent-ai/omnigent/blob/main/CHANGELOG.md"
    )


def test_cli_exports_credits_and_compact_fallback(monkeypatch, tmp_path) -> None:
    result = gen.harvest_pr(1, _body("Fix typo"), "docs: typo", "alice")
    monkeypatch.setattr(gen, "collect", lambda *args, **kwargs: ("section", [result], None))
    credits = tmp_path / "credits.json"
    notes = tmp_path / "notes.md"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate.py",
            "--tag",
            "v1.0.0",
            "--repo",
            _REPO,
            "--credits-out",
            str(credits),
            "--draft-notes-out",
            str(notes),
            "--no-changelog-update",
        ],
    )
    assert gen.main() == 0
    assert json.loads(credits.read_text()) == [
        {"pr": 1, "author": "alice", "author_url": "https://github.com/alice"}
    ]
    assert "## Other contributions" in notes.read_text()
    assert "Fix typo" not in notes.read_text()


def test_draft_notes_omits_empty_sections() -> None:
    # Empty categories do not need a placeholder in complete notes.
    notes = gen.render_draft_notes([_result(1, "x", ["Feature"])], _REPO)
    assert "## Bug fixes" not in notes
    assert "no entries harvested" not in notes


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


def test_all_contributions_have_linked_credits() -> None:
    results = [
        gen.harvest_pr(1, _body("Fix a crash"), "fix: crash", "alice"),
        gen.harvest_pr(2, _body(None).replace("Bug fix", "Docs"), "docs: typo", "bob"),
        gen.harvest_pr(3, "", "chore: update CI", "ci-bot[bot]"),
        gen.harvest_pr(4, None, "Legacy PR", "alice"),
    ]
    notes = gen.render_draft_notes(results, _REPO)
    section = gen.render_section("v1.0.0", "2026-09-24", results, _REPO)
    for text in (notes, section):
        for result in results:
            assert text.count(f"[#{result.pr}](https://github.com/{_REPO}/pull/{result.pr})") == 1
            assert f"[@{result.author}](https://github.com/{result.author})" in text
    for description in ("docs: typo", "chore: update CI", "Legacy PR"):
        assert description in section
        assert description not in notes
    assert "## Other contributions" in notes
    assert "## Documentation updates" not in notes
    assert "(@bob)" in gen.render_pr_list(results)


def test_collect_reads_author_and_preserves_deleted_accounts(monkeypatch) -> None:
    _stub_io(
        monkeypatch,
        subjects=["docs: typo (#1)", "fix: old (#2)", "chore: dependencies (#3)"],
        all_tags=[],
    )
    responses = [
        {"body": "", "author": {"login": "alice"}},
        {"body": "", "author": None},
        {
            "body": "",
            "author": {
                "login": "dependabot[bot]",
                "html_url": "https://github.com/apps/dependabot",
            },
        },
    ]

    def fake_run(command, **kwargs):
        assert command[:2] == ["gh", "api"]
        assert command[2].startswith(f"repos/{_REPO}/pulls/")
        assert command[-1] == "{body, author: .user}"
        assert kwargs["check"] is True
        return subprocess.CompletedProcess(command, 0, json.dumps(responses.pop(0)))

    monkeypatch.setattr(gen.subprocess, "run", fake_run)
    section, results, _ = gen.collect("v1.0.0", _REPO)
    assert results[0].author == "alice"
    assert results[1].author == ""
    assert "[@alice](https://github.com/alice)" in section
    assert "fix: old ([#2](https://github.com/omnigent-ai/omnigent/pull/2))" in section
    assert "[@dependabot[bot]](https://github.com/apps/dependabot)" in section


def test_github_failure_does_not_silently_drop_credits(monkeypatch) -> None:
    _stub_io(monkeypatch, subjects=["docs: typo (#1)"], all_tags=[])

    def fail(command, **kwargs):
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(gen.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        gen.collect("v1.0.0", _REPO)


@pytest.mark.parametrize(
    "stderr",
    [
        "gh: Bad Gateway (HTTP 502)",
        "gh: Too Many Requests (HTTP 429)",
        "gh: API rate limit exceeded (HTTP 403)",
        "read: connection reset by peer",
        "error connecting to api.github.com\ncheck your internet connection or https://githubstatus.com",
        None,
    ],
)
def test_transient_metadata_failures_retry_then_recover(monkeypatch, capsys, stderr) -> None:
    calls = []
    sleeps = []
    metadata = {"body": "", "author": {"login": "alice"}}

    def flaky(command, **kwargs):
        calls.append(command)
        assert kwargs["timeout"] == 30
        if len(calls) < 3:
            if stderr is None:
                raise subprocess.TimeoutExpired(command, 30)
            raise subprocess.CalledProcessError(1, command, stderr=stderr)
        return subprocess.CompletedProcess(command, 0, json.dumps(metadata))

    monkeypatch.setattr(gen.subprocess, "run", flaky)
    monkeypatch.setattr(gen.time, "sleep", sleeps.append)
    assert gen._gh_pr(_REPO, 123) == metadata
    assert len(calls) == 3
    assert sleeps == [1, 2]
    warnings = capsys.readouterr().err
    assert warnings.count("::warning::") == 2
    assert "PR #123" in warnings
    assert "attempt 3/3" in warnings


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_metadata_errors_do_not_retry(monkeypatch, status) -> None:
    calls = []
    sleeps = []

    def fail(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(
            1, command, stderr=f"gh: Request failed (HTTP {status})"
        )

    monkeypatch.setattr(gen.subprocess, "run", fail)
    monkeypatch.setattr(gen.time, "sleep", sleeps.append)
    with pytest.raises(subprocess.CalledProcessError):
        gen._gh_pr(_REPO, 123)
    assert len(calls) == 1
    assert sleeps == []


@pytest.mark.parametrize(
    "stderr",
    [
        "gh: Service unavailable (HTTP 503)",
        "error connecting to api.github.com\ncheck your internet connection or https://githubstatus.com",
    ],
)
def test_exhausted_metadata_retries_still_fail_the_harvest(monkeypatch, stderr) -> None:
    _stub_io(monkeypatch, subjects=["docs: typo (#123)"], all_tags=[])
    calls = []
    sleeps = []

    def fail(command, **kwargs):
        calls.append(command)
        raise subprocess.CalledProcessError(1, command, stderr=stderr)

    monkeypatch.setattr(gen.subprocess, "run", fail)
    monkeypatch.setattr(gen.time, "sleep", sleeps.append)
    with pytest.raises(subprocess.CalledProcessError):
        gen.collect("v1.0.0", _REPO)
    assert len(calls) == 3
    assert sleeps == [1, 2]


def test_collect_skips_verified_issue_references(monkeypatch, capsys) -> None:
    _stub_io(monkeypatch, subjects=["Fix issue (#1)", "Merged PR (#2)"], all_tags=[])
    endpoints = []

    def fake_run(command, **kwargs):
        endpoint = command[2]
        endpoints.append(endpoint)
        if endpoint.endswith("/pulls/1"):
            raise subprocess.CalledProcessError(1, command, stderr="gh: Not Found (HTTP 404)")
        payload = (
            {"number": 1, "pull_request": None}
            if endpoint.endswith("/issues/1")
            else {"body": "", "author": {"login": "alice"}}
        )
        return subprocess.CompletedProcess(command, 0, json.dumps(payload))

    monkeypatch.setattr(gen.subprocess, "run", fake_run)
    section, results, _ = gen.collect("v1.0.0", _REPO)
    assert [result.pr for result in results] == [2]
    assert "[#1]" not in section
    assert "[@alice]" in section
    assert endpoints == [f"repos/{_REPO}/{path}" for path in ("pulls/1", "issues/1", "pulls/2")]
    assert "it is an issue, not a PR" in capsys.readouterr().err


@pytest.mark.parametrize("issue_exists", [True, False])
def test_collect_does_not_hide_unverified_404s(monkeypatch, issue_exists) -> None:
    _stub_io(monkeypatch, subjects=["Merged PR (#1)"], all_tags=[])

    def fake_run(command, **kwargs):
        if command[2].endswith("/issues/1") and issue_exists:
            payload = {
                "number": 1,
                "pull_request": {"url": "https://api.github.com/repos/o/o/pulls/1"},
            }
            return subprocess.CompletedProcess(command, 0, json.dumps(payload))
        raise subprocess.CalledProcessError(1, command, stderr="gh: Not Found (HTTP 404)")

    monkeypatch.setattr(gen.subprocess, "run", fake_run)
    with pytest.raises(subprocess.CalledProcessError):
        gen.collect("v1.0.0", _REPO)


def test_import_generator_outside_script_directory(tmp_path) -> None:
    subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import runpy, sys; runpy.run_path(sys.argv[1])",
            str(SCRIPT),
        ],
        cwd=tmp_path,
        check=True,
    )


def test_prompt_truncation_keeps_complete_utf8_lines() -> None:
    first = "#1: café\n"
    text = first + "#2345: another feature\n"
    for limit in range(len(first.encode("utf-8")), len(text.encode("utf-8"))):
        assert gen.truncate_pr_list(text, limit) == first.rstrip("\n")
    assert gen.truncate_pr_list(text, len(text.encode("utf-8"))) == text
    assert gen.truncate_pr_list(text, len(first.encode("utf-8")) - 2) == ""


def test_prompt_truncation_omits_overlong_first_line() -> None:
    assert gen.truncate_pr_list("#1234: " + "a" * 100, 4) == ""
