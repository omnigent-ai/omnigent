"""Shared Markdown-section parsing for the PR-template tooling.

`validate.py` (the merge gate) and the release-time changelog harvester
(`.github/scripts/changelog/generate.py`) both need to pull a named `##`
section out of a PR body. Keeping that logic in one place means the gate and
the harvester can never drift on what counts as the "## Changelog" section.
"""

from __future__ import annotations

import re

_HEADING_RE = re.compile(r"(?im)^\s*##\s+(.+?)\s*$")
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def strip_html_comments(text: str) -> str:
    """Drop ``<!-- ... -->`` comments (template guidance lives in these)."""
    return _HTML_COMMENT_RE.sub("", text)


def heading_spans(body: str) -> dict[str, tuple[int, int]]:
    """Map each lowercased ``## heading`` to the (start, end) span of its body.

    The span runs from just after the heading line to the start of the next
    ``##`` heading (or end of document). Later duplicate headings win, matching
    the existing validator behaviour.
    """
    matches = list(_HEADING_RE.finditer(body))
    spans: dict[str, tuple[int, int]] = {}
    for idx, match in enumerate(matches):
        title = match.group(1).strip().lower()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        spans[title] = (start, end)
    return spans


def section(body: str, spans: dict[str, tuple[int, int]], heading: str) -> str:
    """Return the raw text under *heading*, or ``""`` if it is absent."""
    span = spans.get(heading.lower())
    if span is None:
        return ""
    return body[span[0] : span[1]]


def section_text(body: str, heading: str) -> str:
    """Convenience: raw text under *heading* parsed straight from *body*."""
    return section(body, heading_spans(body), heading)


# --- "## Changelog" section format ------------------------------------------
#
# Authors write zero or more `<Category>: one-line description` lines, or the
# `skip` sentinel when there's nothing user-facing to announce. The same parser
# backs the PR gate (validate.py) and the release harvester (generate.py).

CHANGELOG_CATEGORIES = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")

_SKIP_SENTINELS = frozenset({"skip", "n/a", "na", "none", "-"})
_ENTRY_RE = re.compile(
    r"(?i)^\s*[-*]?\s*(?P<cat>Added|Changed|Deprecated|Removed|Fixed|Security)"
    r"\s*:\s*(?P<text>.+\S)\s*$"
)


def _content_lines(section_raw: str) -> list[str]:
    return [ln.strip() for ln in strip_html_comments(section_raw).splitlines() if ln.strip()]


def is_changelog_skip(section_raw: str) -> bool:
    """True when the section is empty or only the `skip`/`n/a` sentinel."""
    lines = _content_lines(section_raw)
    if not lines:
        return True
    return all(ln.lstrip("-* ").strip().lower() in _SKIP_SENTINELS for ln in lines)


def parse_changelog_entries(section_raw: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Parse a "## Changelog" section.

    Returns ``(entries, malformed)`` where *entries* is a list of
    ``(canonical_category, description)`` tuples and *malformed* is the list of
    non-blank, non-sentinel lines that did not match ``<Category>: text``.
    """
    entries: list[tuple[str, str]] = []
    malformed: list[str] = []
    for line in _content_lines(section_raw):
        if line.lstrip("-* ").strip().lower() in _SKIP_SENTINELS:
            continue
        match = _ENTRY_RE.match(line)
        if match:
            cat = match.group("cat").lower()
            canonical = next(c for c in CHANGELOG_CATEGORIES if c.lower() == cat)
            entries.append((canonical, match.group("text").strip()))
        else:
            malformed.append(line)
    return entries, malformed
