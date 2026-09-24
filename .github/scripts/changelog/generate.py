#!/usr/bin/env python3
"""Harvest merged-PR "## Changelog" sections into the granular `CHANGELOG.md`.

Run at release time (see `.github/workflows/draft-release-notes.yml`). Given a
final release tag, it:

  1. finds the previous final tag (purely from git — no persisted state),
  2. collects the PRs merged in that range (the `(#NNNN)` suffix on squash
     commits),
  3. reads each PR's author and `## Changelog` section via `gh`, using its title
     when no changelog description was provided,
  4. renders a Keep-a-Changelog section and inserts it into `CHANGELOG.md` in
     version order (idempotent: re-running replaces the version's block).

This is the *granular* tier. The concise website post is produced separately
from the curated GitHub Release body (see `release_to_mdx.py`).

The parsing of the `## Changelog` section is shared with the PR-template gate
(`.github/scripts/pr-template/_md.py`) so the two can never disagree.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from packaging.version import InvalidVersion, Version

# Reuse the exact section + checkbox parsing the merge gate uses.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "pr-template"))
from _md import (
    TYPE_TAGS,
    changelog_description,
    checked_labels,
    section_text,
    type_tag,
)
from compose_notes import compose_notes

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

    def __init__(self, pr: int, title: str = "", author: str = "") -> None:
        self.pr = pr
        self.title = title
        self.author = author
        self.author_url = f"https://github.com/{author}" if author else ""
        self.description = ""  # first-line, free-text changelog description
        self.type_tags: list[str] = []  # checked Type-of-change labels
        self.status = "omitted"  # included | omitted


def harvest_pr(pr: int, body: str | None, title: str = "", author: str = "") -> HarvestResult:
    result = HarvestResult(pr, title, author)
    if body is None:
        return result
    result.description = changelog_description(section_text(body, "Changelog"))
    result.type_tags = sorted(checked_labels(section_text(body, "Type of change"), TYPE_LABELS))
    # Track author-written descriptions separately from title fallbacks.
    if result.description:
        result.status = "included"
    return result


def _credit(result: HarvestResult, repo: str) -> str:
    refs = [f"[#{result.pr}](https://github.com/{repo}/pull/{result.pr})"]
    if result.author:
        refs.append(f"[@{result.author}]({result.author_url})")
    return f"({', '.join(refs)})"


def _bullet(result: HarvestResult, repo: str) -> str:
    """One changelog bullet with its type, linked PR, and author credit."""
    tag = type_tag(set(result.type_tags))
    prefix = f"{tag} " if tag else ""
    description = result.description or result.title or "Untitled pull request"
    return f"- {prefix}{description} {_credit(result, repo)}"


def render_section(tag: str, date: str, results: list[HarvestResult], repo: str) -> str:
    """Render the changelog block for one version — a flat, PR-sorted list.

    Every PR gets a credit, falling back to its title for undocumented changes.
    """
    included = sorted(results, key=lambda r: r.pr)
    lines = [f"## [{tag}] — {date}", ""]
    if included:
        lines.extend(_bullet(r, repo) for r in included)
    else:
        lines.append("_No pull requests in this release._")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def credit_records(results: list[HarvestResult]) -> list[dict]:
    return [{"pr": r.pr, "author": r.author, "author_url": r.author_url} for r in results]


def render_draft_notes(results: list[HarvestResult], repo: str) -> str:
    """Without curated highlights, credit every PR in compact contributor groups."""
    return compose_notes("", credit_records(results), repo)


def truncate_pr_list(text: str, max_bytes: int) -> str:
    """Keep complete UTF-8 lines within the prompt's byte budget."""
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].rpartition(b"\n")[0].decode("utf-8")


def render_pr_list(results: list[HarvestResult]) -> str:
    """Render the PR material fed to the release-notes-drafter agent.

    One line per PR: number, title, and — when the author documented it — the
    type tag and description. Titles come from the squash-commit subjects, so
    even PRs that predate the `## Changelog` field give the agent something to
    theme on.
    """
    lines: list[str] = []
    for result in sorted(results, key=lambda r: r.pr):
        credit = f" (@{result.author})" if result.author else ""
        lines.append(f"#{result.pr}: {result.title or '(no title)'}{credit}")
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


def _retryable_gh_error(error: subprocess.CalledProcessError | subprocess.TimeoutExpired) -> bool:
    if isinstance(error, subprocess.TimeoutExpired):
        return True
    message = (error.stderr or "").lower()
    status = re.search(r"\bhttp (\d{3})\b", message)
    if status:
        code = int(status[1])
        return code in (408, 429) or 500 <= code < 600 or (code == 403 and "rate limit" in message)
    return any(
        marker in message
        for marker in (
            "timeout",
            "timed out",
            "connection reset",
            "connection refused",
            "unexpected eof",
            "tls handshake",
            "temporary failure",
            "no such host",
            "error connecting to ",
        )
    )


def _gh_json(endpoint: str, query: str, context: str) -> dict:
    attempt = 1
    while True:
        try:
            proc = subprocess.run(
                ["gh", "api", endpoint, "--jq", query],
                capture_output=True,
                text=True,
                check=True,
                timeout=30,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as error:
            if attempt >= 3 or not _retryable_gh_error(error):
                raise
            delay = 2 ** (attempt - 1)
            print(
                f"::warning::Transient GitHub metadata failure for {context}; "
                f"retrying in {delay}s (attempt {attempt + 1}/3).",
                file=sys.stderr,
            )
            time.sleep(delay)
            attempt += 1
        else:
            return json.loads(proc.stdout)


def _gh_pr(repo: str, pr: int) -> dict:
    return _gh_json(f"repos/{repo}/pulls/{pr}", "{body, author: .user}", f"PR #{pr}")


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
    results = []
    for pr, title in titles.items():
        try:
            metadata = _gh_pr(repo, pr)
        except subprocess.CalledProcessError as error:
            if re.search(r"\bHTTP 404\b", error.stderr or "", re.IGNORECASE):
                issue = _gh_json(
                    f"repos/{repo}/issues/{pr}", "{number, pull_request}", f"issue #{pr}"
                )
                if issue.get("number") == pr and not issue.get("pull_request"):
                    print(
                        f"::warning::Skipping commit reference #{pr}: it is an issue, not a PR.",
                        file=sys.stderr,
                    )
                    continue
            raise
        author = metadata.get("author") or {}
        result = harvest_pr(pr, metadata.get("body"), title, author.get("login", ""))
        result.author_url = author.get("html_url") or result.author_url
        results.append(result)
    section = render_section(tag, _tag_date(tag), results, repo)
    return section, results, prev


# --- CLI ---------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="release tag/ref (head of the range)")
    parser.add_argument("--repo", required=True, help="GitHub owner/name for PR metadata")
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
        help="optional path to write PR links grouped by contributor "
        "(the GitHub Release body, with optional AI highlights added separately)",
    )
    parser.add_argument("--credits-out", help="path for PR and author metadata as JSON")
    parser.add_argument(
        "--pr-list-out",
        default=None,
        help="optional path to write the PR list (number/title/author/entries) fed to "
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

    if args.credits_out:
        Path(args.credits_out).write_text(json.dumps(credit_records(results)))

    if args.pr_list_out:
        Path(args.pr_list_out).write_text(render_pr_list(results))

    included = [r.pr for r in results if r.status == "included"]
    print(f"Range: {prev or '(start)'}..{args.tag}")
    print(f"Credited all {len(results)} PR(s); {len(included)} have changelog descriptions.")
    print(f"Using PR titles for {len(results) - len(included)} PR(s).")
    return 0


_SEED_CHANGELOG = (
    "# Changelog\n\n"
    "This file is generated at release time from each PR's `## Changelog` section "
    "(or its title), tagged by the PR's `Type of change` (e.g. `[UI]`), with linked "
    "PRs and author credits. The concise, curated highlights live on the website "
    "under `/releases`.\n"
)


if __name__ == "__main__":
    raise SystemExit(main())
