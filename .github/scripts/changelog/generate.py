#!/usr/bin/env python3
"""Harvest merged-PR "## Changelog" sections into the granular `CHANGELOG.md`.

Run at release time (see `.github/workflows/publish-changelog.yml`). Given a
final release tag, it:

  1. finds the previous final tag (purely from git — no persisted state),
  2. collects the PRs merged in that range (the `(#NNNN)` suffix on squash
     commits),
  3. reads each PR's `## Changelog` section via `gh`,
  4. renders a Keep-a-Changelog section and inserts it into `CHANGELOG.md` in
     version order (idempotent: re-running replaces the version's block).

This is the *granular* tier. The concise website post is produced separately
from the curated GitHub Release body (see `release_to_mdx.py`).

The parsing of the `## Changelog` section is shared with the PR-template gate
(`.github/scripts/pr-template/_md.py`) so the two can never disagree.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Reuse the exact section + changelog parsing the merge gate uses.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pr-template"))
from _md import (
    CHANGELOG_CATEGORIES,
    is_changelog_skip,
    parse_changelog_entries,
    section_text,
)

_FINAL_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
# A squash-merge subject ends with "(#1234)"; capture the last such reference.
_PR_REF_RE = re.compile(r"\(#(\d+)\)\s*$")
# Existing version headers in CHANGELOG.md, e.g. "## [v0.3.0] — 2026-06-27".
_VERSION_HEADER_RE = re.compile(r"(?m)^##\s*\[v(\d+)\.(\d+)\.(\d+)\]")


# --- version helpers (final vX.Y.Z only → plain integer-tuple ordering) -------


def _version_tuple(tag: str) -> tuple[int, int, int] | None:
    match = _FINAL_TAG_RE.match(tag.strip())
    if not match:
        return None
    return tuple(int(p) for p in match.groups())  # type: ignore[return-value]


def previous_final_tag(tag: str, all_tags: list[str]) -> str | None:
    """Highest final tag strictly below *tag*, or ``None`` if there is none."""
    current = _version_tuple(tag)
    if current is None:
        raise ValueError(f"{tag!r} is not a final vX.Y.Z tag")
    below = [
        (version, candidate)
        for candidate in all_tags
        if (version := _version_tuple(candidate)) is not None and version < current
    ]
    if not below:
        return None
    return max(below)[1]


def pr_numbers_from_subjects(subjects: list[str]) -> list[int]:
    """PR numbers from squash-commit subjects, de-duplicated, first-seen order."""
    return list(pr_titles_from_subjects(subjects))


def pr_titles_from_subjects(subjects: list[str]) -> dict[int, str]:
    """Map PR number -> title from squash-commit subjects (first seen wins).

    A squash subject looks like ``feat(web): show progress bar (#1304)``; the
    title is the subject with the trailing ``(#NNNN)`` reference stripped.
    """
    titles: dict[int, str] = {}
    for subject in subjects:
        match = _PR_REF_RE.search(subject)
        if not match:
            continue
        pr = int(match.group(1))
        if pr in titles:
            continue
        titles[pr] = _PR_REF_RE.sub("", subject).strip()
    return titles


# --- rendering ---------------------------------------------------------------


class HarvestResult:
    """Per-PR harvest outcome, for rendering and for surfacing gaps."""

    def __init__(self, pr: int, title: str = "") -> None:
        self.pr = pr
        self.title = title
        self.entries: list[tuple[str, str]] = []  # (category, text)
        self.status = "skip"  # skip | included | no-section | unparseable


def harvest_pr(pr: int, body: str | None, title: str = "") -> HarvestResult:
    result = HarvestResult(pr, title)
    if body is None:
        result.status = "no-section"
        return result
    if "changelog" not in _headings(body):
        result.status = "no-section"
        return result
    raw = section_text(body, "Changelog")
    if is_changelog_skip(raw):
        result.status = "skip"
        return result
    entries, malformed = parse_changelog_entries(raw)
    result.entries = entries
    result.status = "included" if entries else ("unparseable" if malformed else "skip")
    return result


def _headings(body: str) -> set[str]:
    return {m.group(1).strip().lower() for m in re.finditer(r"(?im)^\s*##\s+(.+?)\s*$", body)}


def render_section(tag: str, date: str, results: list[HarvestResult]) -> str:
    """Render the Keep-a-Changelog block for one version."""
    by_category: dict[str, list[tuple[int, str]]] = {c: [] for c in CHANGELOG_CATEGORIES}
    for result in results:
        for category, text in result.entries:
            by_category[category].append((result.pr, text))

    lines = [f"## [{tag}] — {date}", ""]
    any_entries = False
    for category in CHANGELOG_CATEGORIES:
        items = sorted(by_category[category])
        if not items:
            continue
        any_entries = True
        lines.append(f"### {category}")
        for pr, text in items:
            lines.append(f"- {text} (#{pr})")
        lines.append("")
    if not any_entries:
        lines.append("_No user-facing changes._")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# Two-section draft for the GitHub Release body: the six Keep-a-Changelog
# categories collapse into the two buckets the release coordinator curates by
# hand (see RELEASING.md / the release-notes-drafter agent). This is the
# deterministic scaffold — the AI drafter refines it, and it is also the
# fallback when the LLM is unavailable.
DRAFT_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Major new features", ("Added", "Changed")),
    ("Bug fixes & hardening", ("Fixed", "Security", "Removed", "Deprecated")),
)


def render_draft_notes(results: list[HarvestResult], repo: str) -> str:
    """Render the two-section curated-draft scaffold for the GitHub Release body.

    Groups the harvested one-liners into "Major new features" and "Bug fixes &
    hardening", sorted by PR number, and appends the CHANGELOG.md link. Empty
    sections keep their heading with a placeholder so the coordinator sees what
    to fill in.
    """
    by_category: dict[str, list[tuple[int, str]]] = {c: [] for c in CHANGELOG_CATEGORIES}
    for result in results:
        for category, text in result.entries:
            by_category[category].append((result.pr, text))

    lines: list[str] = []
    for heading, categories in DRAFT_SECTIONS:
        lines.append(f"## {heading}")
        lines.append("")
        items = sorted({item for cat in categories for item in by_category[cat]})
        if items:
            for pr, text in items:
                lines.append(f"- {text} (#{pr})")
        else:
            lines.append("<!-- no entries harvested for this section — add highlights -->")
        lines.append("")

    lines.append(f"Full Changelog: https://github.com/{repo}/blob/main/CHANGELOG.md")
    return "\n".join(lines).rstrip() + "\n"


def render_pr_list(results: list[HarvestResult]) -> str:
    """Render the PR material fed to the release-notes-drafter agent.

    One line per PR: number, title, and the author-written changelog entries
    (if any). Titles come from the squash-commit subjects, so even PRs that
    predate the `## Changelog` field still give the agent something to theme on.
    """
    lines: list[str] = []
    for result in sorted(results, key=lambda r: r.pr):
        lines.append(f"#{result.pr}: {result.title or '(no title)'}")
        for category, text in result.entries:
            lines.append(f"    - [{category}] {text}")
    return "\n".join(lines) + "\n"


def insert_section(changelog: str, tag: str, section: str) -> str:
    """Insert (or replace) *section* for *tag* into *changelog*, version-ordered.

    Newest version first. If the tag is already present its block is replaced,
    making re-runs idempotent.
    """
    target = _version_tuple(tag)
    if target is None:
        raise ValueError(f"{tag!r} is not a final vX.Y.Z tag")

    headers = list(_VERSION_HEADER_RE.finditer(changelog))
    blocks = []  # (version_tuple, start, end)
    for idx, match in enumerate(headers):
        version = tuple(int(g) for g in match.groups())
        start = match.start()
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(changelog)
        blocks.append((version, start, end))

    section_block = section.rstrip() + "\n"

    # Replace an existing block for this exact version.
    for version, start, end in blocks:
        if version == target:
            return changelog[:start] + section_block + "\n" + changelog[end:].lstrip("\n")

    # Otherwise insert before the first existing version that is older than ours.
    for version, start, _end in blocks:
        if version < target:
            head = changelog[:start].rstrip("\n")
            tail = changelog[start:]
            return f"{head}\n\n{section_block}\n{tail}"

    # No older block (we're the oldest, or the file has no version blocks yet):
    # append after the preamble / existing blocks.
    return changelog.rstrip("\n") + "\n\n" + section_block


# --- git / gh IO -------------------------------------------------------------


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def _all_tags() -> list[str]:
    out = _git("tag", "-l", "v*")
    return [line.strip() for line in out.splitlines() if line.strip()]


def _range_subjects(prev: str | None, tag: str) -> list[str]:
    rng = f"{prev}..{tag}" if prev else tag
    out = _git("log", "--no-merges", "--pretty=%s", rng)
    return [line for line in out.splitlines() if line.strip()]


def _tag_date(tag: str) -> str:
    return _git("log", "-1", "--format=%cs", tag)


def _gh_pr_body(repo: str, pr: int) -> str | None:
    proc = subprocess.run(
        ["gh", "pr", "view", str(pr), "--repo", repo, "--json", "body", "-q", ".body"],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def collect(tag: str, repo: str) -> tuple[str, list[HarvestResult], str | None]:
    """Return (rendered_section, results, previous_tag) for *tag*."""
    prev = previous_final_tag(tag, _all_tags())
    subjects = _range_subjects(prev, tag)
    titles = pr_titles_from_subjects(subjects)
    results = [harvest_pr(pr, _gh_pr_body(repo, pr), title) for pr, title in titles.items()]
    section = render_section(tag, _tag_date(tag), results)
    return section, results, prev


# --- CLI ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="final release tag, e.g. v0.3.0")
    parser.add_argument("--repo", required=True, help="owner/name for `gh pr view`")
    parser.add_argument(
        "--changelog-file",
        default="CHANGELOG.md",
        help="path to the canonical CHANGELOG.md to update in place",
    )
    parser.add_argument(
        "--section-out",
        default=None,
        help="optional path to also write the rendered section on its own",
    )
    parser.add_argument(
        "--draft-notes-out",
        default=None,
        help="optional path to write the two-section curated-draft scaffold "
        "(the GitHub Release body seed / LLM fallback)",
    )
    parser.add_argument(
        "--pr-list-out",
        default=None,
        help="optional path to write the PR list (number/title/entries) fed to "
        "the release-notes-drafter agent",
    )
    parser.add_argument(
        "--no-changelog-update",
        action="store_true",
        help="skip writing CHANGELOG.md (useful when only the draft notes are wanted)",
    )
    args = parser.parse_args()

    section, results, prev = collect(args.tag, args.repo)

    if not args.no_changelog_update:
        path = Path(args.changelog_file)
        existing = path.read_text() if path.exists() else _SEED_CHANGELOG
        path.write_text(insert_section(existing, args.tag, section))

    if args.section_out:
        Path(args.section_out).write_text(section)

    if args.draft_notes_out:
        Path(args.draft_notes_out).write_text(render_draft_notes(results, args.repo))

    if args.pr_list_out:
        Path(args.pr_list_out).write_text(render_pr_list(results))

    # Surface gaps so a maintainer can backfill (non-fatal).
    included = [r.pr for r in results if r.status == "included"]
    skipped = [r.pr for r in results if r.status == "skip"]
    missing = [r.pr for r in results if r.status == "no-section"]
    unparseable = [r.pr for r in results if r.status == "unparseable"]
    print(f"Range: {prev or '(start)'}..{args.tag}")
    print(f"Included {len(included)} entr(y/ies) from PRs: {included}")
    print(f"Skipped (explicit `skip`): {skipped}")
    if missing:
        print(f"::warning::PRs with no `## Changelog` section: {missing}")
    if unparseable:
        print(f"::warning::PRs with unparseable `## Changelog`: {unparseable}")
    return 0


_SEED_CHANGELOG = (
    "# Changelog\n\n"
    "All notable user-facing changes to omnigent are documented here. This file is "
    "generated at release time from each PR's `## Changelog` section; the concise, "
    "curated highlights live on the website under `/releases`.\n\n"
    "The format follows [Keep a Changelog](https://keepachangelog.com/).\n"
)


if __name__ == "__main__":
    raise SystemExit(main())
