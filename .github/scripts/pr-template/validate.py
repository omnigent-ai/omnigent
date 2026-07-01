#!/usr/bin/env python3
"""Validate that a PR description follows the repository template.

The GitHub workflow passes the PR body in PR_BODY. The script is also
unit-tested directly so changes to the template gate are reviewed like
normal code.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Share the Markdown-section + changelog parsing with the release-time harvester
# (.github/scripts/changelog/generate.py) so the gate and the harvester can
# never disagree on what the "## Changelog" section means.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from _md import (
    CHANGELOG_CATEGORIES,
    is_changelog_skip,
    parse_changelog_entries,
)
from _md import heading_spans as _heading_spans
from _md import section as _section
from _md import strip_html_comments as _strip_html_comments

REQUIRED_HEADINGS = (
    "Summary",
    "Test Plan",
    "Type of change",
    "Test coverage",
    "Changelog",
)

TYPE_LABELS = (
    "Bug fix",
    "Feature",
    "UI / frontend change",
    "Refactor / chore",
    "Docs",
    "Test / CI",
    "Breaking change",
)

TEST_LABELS = (
    "Unit tests added / updated",
    "Integration tests added / updated",
    "E2E tests added / updated",
    "Manual verification completed",
    "Existing tests cover this change",
    "Not applicable",
)

PLACEHOLDER_FRAGMENTS = (
    "what changed and why",
    "check all that apply",
    "describe below",
    "how was this change tested",
)


class ValidationResult:
    def __init__(self, ok: bool, errors: list[str]) -> None:
        self.ok = ok
        self.errors = errors


_CHECKBOX_RE = re.compile(r"(?im)^\s*-\s*\[(?P<mark>[ xX])\]\s*(?P<label>.+?)\s*$")


def _checked_labels(section: str, expected_labels: tuple[str, ...]) -> set[str]:
    expected_by_lower = {label.lower(): label for label in expected_labels}
    checked: set[str] = set()
    for match in _CHECKBOX_RE.finditer(section):
        label = match.group("label").strip()
        canonical = expected_by_lower.get(label.lower())
        if canonical and match.group("mark").lower() == "x":
            checked.add(canonical)
    return checked


def _missing_labels(section: str, expected_labels: tuple[str, ...]) -> list[str]:
    present = {match.group("label").strip().lower() for match in _CHECKBOX_RE.finditer(section)}
    return [label for label in expected_labels if label.lower() not in present]


def _meaningful_text(section: str) -> str:
    text = _strip_html_comments(section)
    text = re.sub(r"(?im)^\s*-\s*\[[ xX]\].*$", "", text)
    return text.strip()


def _contains_placeholder(text: str) -> bool:
    lowered = text.lower()
    return any(fragment in lowered for fragment in PLACEHOLDER_FRAGMENTS)


def validate_pr_body(body: str) -> ValidationResult:
    body = body.lstrip("\ufeff")
    errors: list[str] = []

    spans = _heading_spans(body)
    for heading in REQUIRED_HEADINGS:
        if heading.lower() not in spans:
            errors.append(f"Missing required section: ## {heading}")

    summary = _meaningful_text(_section(body, spans, "Summary"))
    if not summary:
        errors.append("Summary must describe what changed and why.")
    elif _contains_placeholder(summary):
        errors.append("Summary still contains template placeholder text.")

    test_plan = _meaningful_text(_section(body, spans, "Test Plan"))
    if not test_plan:
        errors.append("Test Plan must describe how the change was tested.")
    elif _contains_placeholder(test_plan):
        errors.append("Test Plan still contains template placeholder text.")

    type_section = _section(body, spans, "Type of change")
    missing_type_labels = _missing_labels(type_section, TYPE_LABELS)
    if missing_type_labels:
        errors.append(
            "Type of change is missing template checkbox(es): " + ", ".join(missing_type_labels)
        )
    checked_types = _checked_labels(type_section, TYPE_LABELS)
    if not checked_types:
        errors.append("Check at least one Type of change checkbox.")

    # The Demo section is mandatory for UI / frontend changes — reviewers need
    # a screenshot or recording of the new behaviour. It stays optional for
    # everything else.
    if "UI / frontend change" in checked_types:
        demo = _meaningful_text(_section(body, spans, "Demo"))
        if not demo:
            errors.append(
                "Demo is required for UI / frontend changes — attach a screenshot "
                "or screen recording demonstrating the new behaviour."
            )
        elif _contains_placeholder(demo):
            errors.append("Demo still contains template placeholder text.")

    test_section = _section(body, spans, "Test coverage")
    missing_test_labels = _missing_labels(test_section, TEST_LABELS)
    if missing_test_labels:
        errors.append(
            "Test coverage is missing template checkbox(es): " + ", ".join(missing_test_labels)
        )
    checked_tests = _checked_labels(test_section, TEST_LABELS)
    if not checked_tests:
        errors.append("Check at least one Test coverage checkbox.")

    # Coverage notes are optional in general, but required whenever "Manual
    # verification completed" or "Not applicable" is checked — those choices
    # need a written justification.
    if checked_tests & {"Manual verification completed", "Not applicable"}:
        coverage_notes = _meaningful_text(_section(body, spans, "Coverage notes"))
        if not coverage_notes:
            errors.append(
                "Coverage notes are required when 'Manual verification completed' or "
                "'Not applicable' is selected — describe what you verified or why "
                "automated coverage is not needed."
            )
        elif _contains_placeholder(coverage_notes):
            errors.append("Coverage notes still contains template placeholder text.")

    # Changelog feeds the release-time CHANGELOG.md harvester, so it must be the
    # `skip` sentinel or one or more `<Category>: description` lines it can parse
    # deterministically. A breaking change must always carry an entry — those are
    # exactly what users need announced.
    if "changelog" in spans:
        changelog_section = _section(body, spans, "Changelog")
        if is_changelog_skip(changelog_section):
            if "Breaking change" in checked_types:
                errors.append(
                    "Changelog must not be 'skip' when 'Breaking change' is checked "
                    "— add a '<Category>: description' line announcing it."
                )
        else:
            _entries, malformed = parse_changelog_entries(changelog_section)
            if malformed:
                errors.append(
                    "Changelog lines must be 'skip' or '<Category>: description' "
                    f"(Category one of: {', '.join(CHANGELOG_CATEGORIES)}). "
                    "Offending line(s): " + "; ".join(malformed)
                )

    return ValidationResult(ok=not errors, errors=errors)


def main() -> int:
    body = os.environ["PR_BODY"]
    result = validate_pr_body(body)
    if result.ok:
        print("PR template validation passed.")
        return 0

    print("PR template validation failed:")
    for error in result.errors:
        print(f"- {error}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
