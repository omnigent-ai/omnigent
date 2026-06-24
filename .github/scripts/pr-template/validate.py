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

REQUIRED_HEADINGS = (
    "Summary",
    "Test Plan",
    "Type of change",
    "Test coverage",
)

TYPE_LABELS = (
    "Bug fix",
    "Feature",
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


_HEADING_RE = re.compile(r"(?im)^\s*##\s+(.+?)\s*$")
_CHECKBOX_RE = re.compile(r"(?im)^\s*-\s*\[(?P<mark>[ xX])\]\s*(?P<label>.+?)\s*$")


def _strip_html_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def _heading_spans(body: str) -> dict[str, tuple[int, int]]:
    matches = list(_HEADING_RE.finditer(body))
    spans: dict[str, tuple[int, int]] = {}
    for idx, match in enumerate(matches):
        title = match.group(1).strip().lower()
        start = match.end()
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(body)
        spans[title] = (start, end)
    return spans


def _section(body: str, spans: dict[str, tuple[int, int]], heading: str) -> str:
    span = spans.get(heading.lower())
    if span is None:
        return ""
    return body[span[0] : span[1]]


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
