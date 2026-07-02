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
_CHECKBOX_RE = re.compile(r"(?im)^\s*-\s*\[(?P<mark>[ xX])\]\s*(?P<label>.+?)\s*$")


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


# --- checkbox parsing (shared by the gate and the harvester) ----------------


def checked_labels(section_raw: str, expected_labels: tuple[str, ...]) -> set[str]:
    """Return the canonical labels whose checkbox is ticked in *section_raw*."""
    expected_by_lower = {label.lower(): label for label in expected_labels}
    checked: set[str] = set()
    for match in _CHECKBOX_RE.finditer(section_raw):
        label = match.group("label").strip()
        canonical = expected_by_lower.get(label.lower())
        if canonical and match.group("mark").lower() == "x":
            checked.add(canonical)
    return checked


# --- "## Changelog" section format ------------------------------------------
#
# The section holds a free-text, user-voice one-liner describing the change (the
# author may hard-wrap it — we take the first line). The category/tag is NOT
# written here; it is derived from the "Type of change" checkboxes via TYPE_TAGS.
# The section is optional: an author deletes it (or leaves the `<…>` placeholder)
# when the change isn't noteworthy, and the PR is then omitted from the changelog.
# The same parser backs the PR gate (validate.py) and the harvester (generate.py).

# "Type of change" checkbox label -> bracket tag rendered in CHANGELOG.md.
TYPE_TAGS: dict[str, str] = {
    "UI / frontend change": "UI",
    "Bug fix": "Bug fix",
    "Feature": "Feature",
    "Docs": "Docs",
    "Refactor / chore": "Chore",
    "Test / CI": "Test/CI",
    "Breaking change": "Breaking",
}

_PLACEHOLDER_RE = re.compile(r"^\s*<.*>\s*$")
# Markers meaning "nothing to announce" — the section is optional and deletable,
# but authors (and the old template's `skip` sentinel) still write these; treat
# them as an absent section rather than leaking them in as literal entries.
_OMIT_MARKERS = frozenset({"skip", "n/a", "na", "none", "-"})


def is_placeholder(line: str) -> bool:
    """True when *line* is the untouched ``<…>`` template placeholder."""
    return bool(_PLACEHOLDER_RE.match(line))


def changelog_description(section_raw: str) -> str:
    """First meaningful line of a "## Changelog" section.

    Strips HTML comments, then returns the first non-blank line — unless that
    line is the ``<…>`` placeholder or an omit marker (``skip``/``n/a``/…), in
    which case the section counts as absent and this returns ``""``. Multi-line /
    wrapped bodies collapse to their first line.
    """
    for raw in strip_html_comments(section_raw).splitlines():
        line = raw.strip()
        if not line:
            continue
        if is_placeholder(line) or line.lower() in _OMIT_MARKERS:
            return ""
        return line
    return ""


def type_tag(labels: set[str]) -> str:
    """Render the bracket tag for the checked Type-of-change *labels*.

    Joined with ` / ` in TYPE_TAGS declaration order (e.g. ``[UI / Bug fix]``).
    Returns ``""`` when no known type is checked.
    """
    tags = [tag for label, tag in TYPE_TAGS.items() if label in labels]
    return f"[{' / '.join(tags)}]" if tags else ""
