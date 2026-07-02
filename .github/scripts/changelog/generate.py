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

from packaging.version import InvalidVersion, Version

# Reuse the exact section + checkbox parsing the merge gate uses.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pr-template"))
from _md import (
    TYPE_TAGS,
    changelog_description,
    checked_labels,
    section_text,
    type_tag,
)

# The "Type of change" checkbox labels, in the order they appear in the template
# (mirrors validate.TYPE_LABELS). Kept here so the harvester needn't import the
# gate module; TYPE_TAGS in _md.py is the source of truth for which map to a tag.
TYPE_LABELS = tuple(TYPE_TAGS)

_FINAL_TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
# A squash-merge subject ends with "(#1234)"; capture the last such reference.
_PR_REF_RE = re.compile(r"\(#(\d+)\)\s*$")
# Existing version headers in CHANGELOG.md — capture the whole bracketed tag so
# any version shape (final, rc, dev) is found, e.g. "## [v0.4.0rc1] — 2026-…".
_VERSION_HEADER_RE = re.compile(r"(?m)^##\s*\[([^\]]+)\]")


# --- version helpers ---------------------------------------------------------
#
# Two notions, deliberately distinct:
#   * FINALITY (_version_tuple / previous_final_tag): only vX.Y.Z. Governs the
#     default range start — a real v0.4.0 diffs against the previous *final* tag
#     (v0.3.0), never an intervening v0.4.0rc1.
#   * ORDERABILITY (_parse_version): any PEP 440 version, incl. dev/rc. Governs
#     where a block sorts in CHANGELOG.md, so a manually-drafted dev/rc tag lands
#     in the right place (and below its eventual final).


def _version_tuple(tag: str) -> tuple[int, int, int] | None:
    match = _FINAL_TAG_RE.match(tag.strip())
    if not match:
        return None
    return tuple(int(p) for p in match.groups())  # type: ignore[return-value]


def _parse_version(tag: str) -> Version | None:
    """PEP 440 version for *tag* (leading ``v`` stripped), or ``None`` if it isn't
    a version at all (e.g. a branch/sha). ``Version`` sorts dev < rc < final."""
    try:
        return Version(tag.strip().lstrip("v"))
    except InvalidVersion:
        return None


def previous_final_tag(tag: str, all_tags: list[str]) -> str | None:
    """Highest *final* (vX.Y.Z) tag strictly below *tag*, or ``None`` if none.

    The reference *tag* may itself be any PEP 440 version (a dev/rc tag drafted
    manually still diffs against the previous final release); only the candidates
    are restricted to finals.
    """
    current = _parse_version(tag)
    if current is None:
        raise ValueError(f"{tag!r} is not a PEP 440 version")
    below = [
        (version, candidate)
        for candidate in all_tags
        if _version_tuple(candidate) is not None
        and (version := _parse_version(candidate)) is not None
        and version < current
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
        self.description = ""  # first-line, free-text changelog description
        self.type_tags: list[str] = []  # checked Type-of-change labels
        self.status = "omitted"  # included | omitted


def harvest_pr(pr: int, body: str | None, title: str = "") -> HarvestResult:
    result = HarvestResult(pr, title)
    if body is None:
        return result
    result.description = changelog_description(section_text(body, "Changelog"))
    result.type_tags = sorted(checked_labels(section_text(body, "Type of change"), TYPE_LABELS))
    # A PR is in the changelog iff its author wrote a description line; the tag
    # comes from the Type-of-change boxes but never puts a PR in on its own.
    if result.description:
        result.status = "included"
    return result


def _bullet(result: HarvestResult) -> str:
    """One CHANGELOG.md bullet: ``- [Tag] description (#NNNN)`` (tag optional)."""
    tag = type_tag(set(result.type_tags))
    prefix = f"{tag} " if tag else ""
    return f"- {prefix}{result.description} (#{result.pr})"


def render_section(tag: str, date: str, results: list[HarvestResult]) -> str:
    """Render the changelog block for one version — a flat, PR-sorted list.

    Each documented PR is one bullet prefixed with the bracket tag derived from
    its Type-of-change checkboxes. PRs with no description are omitted entirely.
    """
    included = sorted((r for r in results if r.status == "included"), key=lambda r: r.pr)
    lines = [f"## [{tag}] — {date}", ""]
    if included:
        lines.extend(_bullet(r) for r in included)
    else:
        lines.append("_No user-facing changes._")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# Two-section draft for the GitHub Release body: the Type-of-change tags collapse
# into the two buckets the release coordinator curates by hand (see RELEASING.md /
# the release-notes-drafter agent). This is the deterministic scaffold — the AI
# drafter refines it, and it is also the fallback when the LLM is unavailable.
# Values are "Type of change" checkbox labels (see _md.TYPE_TAGS).
DRAFT_SECTIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Major new features", ("Feature", "UI / frontend change")),
    ("Bug fixes & hardening", ("Bug fix", "Breaking change")),
)


def render_draft_notes(results: list[HarvestResult], repo: str) -> str:
    """Render the two-section curated-draft scaffold for the GitHub Release body.

    Groups documented PRs into "Major new features" and "Bug fixes & hardening"
    by their Type-of-change labels, sorted by PR number, and appends the
    CHANGELOG.md link. Empty sections keep their heading with a placeholder so
    the coordinator sees what to fill in.
    """
    included = [r for r in results if r.status == "included"]

    lines: list[str] = []
    for heading, labels in DRAFT_SECTIONS:
        lines.append(f"## {heading}")
        lines.append("")
        bucket = sorted(
            (r for r in included if any(label in r.type_tags for label in labels)),
            key=lambda r: r.pr,
        )
        if bucket:
            lines.extend(f"- {r.description} (#{r.pr})" for r in bucket)
        else:
            lines.append("<!-- no entries harvested for this section — add highlights -->")
        lines.append("")

    lines.append(f"Full Changelog: https://github.com/{repo}/blob/main/CHANGELOG.md")
    return "\n".join(lines).rstrip() + "\n"


def render_pr_list(results: list[HarvestResult]) -> str:
    """Render the PR material fed to the release-notes-drafter agent.

    One line per PR: number, title, and — when the author documented it — the
    type tag and description. Titles come from the squash-commit subjects, so
    even PRs that predate the `## Changelog` field give the agent something to
    theme on.
    """
    lines: list[str] = []
    for result in sorted(results, key=lambda r: r.pr):
        lines.append(f"#{result.pr}: {result.title or '(no title)'}")
        if result.description:
            tag = type_tag(set(result.type_tags))
            prefix = f"{tag} " if tag else ""
            lines.append(f"    - {prefix}{result.description}")
    return "\n".join(lines) + "\n"


def insert_section(changelog: str, tag: str, section: str) -> str:
    """Insert (or replace) *section* for *tag* into *changelog*, version-ordered.

    Newest version first, by PEP 440 — so a final ``v0.4.0`` sorts above its own
    ``v0.4.0rc1`` / ``v0.4.0.dev0`` blocks, which in turn sort above ``v0.3.0``.
    Re-running the same tag replaces its own block (matched by exact tag string),
    making re-runs idempotent; distinct tags (final vs. its pre-releases) coexist.
    """
    target = _parse_version(tag)
    if target is None:
        raise ValueError(f"{tag!r} is not a PEP 440 version")

    headers = list(_VERSION_HEADER_RE.finditer(changelog))
    blocks = []  # (header_tag, parsed_version_or_None, start, end)
    for idx, match in enumerate(headers):
        header_tag = match.group(1).strip()
        start = match.start()
        end = headers[idx + 1].start() if idx + 1 < len(headers) else len(changelog)
        blocks.append((header_tag, _parse_version(header_tag), start, end))

    section_block = section.rstrip() + "\n"

    # Replace an existing block for this exact tag (idempotent re-run).
    for header_tag, _version, start, end in blocks:
        if header_tag == tag.strip():
            return changelog[:start] + section_block + "\n" + changelog[end:].lstrip("\n")

    # Otherwise insert before the first existing block that sorts below ours. An
    # unparseable existing header is treated as oldest (sorts last).
    for _header_tag, version, start, _end in blocks:
        if version is None or version < target:
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


def collect(
    tag: str, repo: str, base: str | None = None
) -> tuple[str, list[HarvestResult], str | None]:
    """Return (rendered_section, results, previous_tag) for *tag*.

    *base* overrides the range start: when given, the harvest range is
    ``base..tag`` verbatim (any refs — for manual/preview runs). Otherwise the
    start is the previous final ``vX.Y.Z`` tag, as at release time.
    """
    prev = base or previous_final_tag(tag, _all_tags())
    subjects = _range_subjects(prev, tag)
    titles = pr_titles_from_subjects(subjects)
    results = [harvest_pr(pr, _gh_pr_body(repo, pr), title) for pr, title in titles.items()]
    section = render_section(tag, _tag_date(tag), results)
    return section, results, prev


# --- CLI ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="release tag/ref (head of the range)")
    parser.add_argument("--repo", required=True, help="owner/name for `gh pr view`")
    parser.add_argument(
        "--base",
        default=None,
        help="override the range start (any ref); default is the previous final "
        "vX.Y.Z tag. Required when --tag is not a final vX.Y.Z (e.g. a preview run).",
    )
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

    # CHANGELOG.md insertion orders blocks by PEP 440, so --tag must be a version
    # (final, rc, or dev — all orderable). A non-version ref (branch/sha) can only
    # render a preview, and needs an explicit --base for its range.
    is_orderable = _parse_version(args.tag) is not None
    if not is_orderable and args.base is None:
        parser.error(
            f"--tag {args.tag!r} is not a PEP 440 version; pass --base <ref> for its range"
        )

    section, results, prev = collect(args.tag, args.repo, base=args.base)

    if is_orderable and not args.no_changelog_update:
        path = Path(args.changelog_file)
        existing = path.read_text() if path.exists() else _SEED_CHANGELOG
        path.write_text(insert_section(existing, args.tag, section))

    if args.section_out:
        Path(args.section_out).write_text(section)

    if args.draft_notes_out:
        Path(args.draft_notes_out).write_text(render_draft_notes(results, args.repo))

    if args.pr_list_out:
        Path(args.pr_list_out).write_text(render_pr_list(results))

    # Summarize what landed (non-fatal). PRs without a description line are simply
    # omitted from the changelog by design — no per-PR gap warnings.
    included = [r.pr for r in results if r.status == "included"]
    print(f"Range: {prev or '(start)'}..{args.tag}")
    print(f"Documented {len(included)} of {len(results)} PR(s) in the changelog: {included}")
    print(f"Omitted (no changelog description): {len(results) - len(included)} PR(s).")
    return 0


_SEED_CHANGELOG = (
    "# Changelog\n\n"
    "All notable user-facing changes to omnigent are documented here. This file is "
    "generated at release time from each PR's `## Changelog` section, tagged by the "
    "PR's `Type of change` (e.g. `[UI]`); the concise, curated highlights live on "
    "the website under `/releases`.\n"
)


if __name__ == "__main__":
    raise SystemExit(main())
