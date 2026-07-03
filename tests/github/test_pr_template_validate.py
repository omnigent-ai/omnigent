from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parents[2] / ".github" / "scripts" / "pr-template" / "validate.py"
)
spec = importlib.util.spec_from_file_location("validate_pr_template", SCRIPT)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def _valid_body(
    *,
    summary: str = "- Improves the agent handoff flow and fixes stale polling.",
    test_plan: str = (
        "Added focused unit coverage for the cursor math and an E2E regression "
        "that exercises the REPL path."
    ),
    type_checkboxes: str = """
- [x] Bug fix
- [ ] Feature
- [ ] UI / frontend change
- [ ] Refactor / chore
- [ ] Docs
- [ ] Test / CI
- [ ] Breaking change
""",
    test_checkboxes: str = """
- [x] Unit tests added / updated
- [ ] Integration tests added / updated
- [x] E2E tests added / updated
- [ ] Manual verification completed
- [ ] Existing tests cover this change
- [ ] Not applicable
""",
    coverage_notes: str | None = None,
    demo: str | None = None,
    changelog: str | None = "Fixed: stale polling no longer blocks the REPL handoff",
) -> str:
    notes_section = "" if coverage_notes is None else f"\n## Coverage notes\n\n{coverage_notes}\n"
    demo_section = "" if demo is None else f"\n## Demo\n\n{demo}\n"
    changelog_section = "" if changelog is None else f"\n## Changelog\n\n{changelog}\n"
    return f"""
## Summary

{summary}

## Test Plan

{test_plan}
{demo_section}
## Type of change
{type_checkboxes}
## Test coverage
{test_checkboxes}{notes_section}{changelog_section}"""


_BREAKING_TYPE = """
- [ ] Bug fix
- [ ] Feature
- [ ] UI / frontend change
- [ ] Refactor / chore
- [ ] Docs
- [ ] Test / CI
- [x] Breaking change
"""


_UI_CHANGE = """
- [ ] Bug fix
- [ ] Feature
- [x] UI / frontend change
- [ ] Refactor / chore
- [ ] Docs
- [ ] Test / CI
- [ ] Breaking change
"""


_MANUAL_ONLY = """
- [ ] Unit tests added / updated
- [ ] Integration tests added / updated
- [ ] E2E tests added / updated
- [x] Manual verification completed
- [ ] Existing tests cover this change
- [ ] Not applicable
"""

_NOT_APPLICABLE_ONLY = """
- [ ] Unit tests added / updated
- [ ] Integration tests added / updated
- [ ] E2E tests added / updated
- [ ] Manual verification completed
- [ ] Existing tests cover this change
- [x] Not applicable
"""


def test_valid_body() -> None:
    result = module.validate_pr_body(_valid_body())
    assert result.ok, result.errors


def test_validate_pr_body_accepts_leading_bom() -> None:
    result = module.validate_pr_body("﻿" + _valid_body())
    assert result.ok, result.errors


def test_requires_type_and_test_checkboxes() -> None:
    body = _valid_body(
        type_checkboxes="""
- [ ] Bug fix
- [ ] Feature
- [ ] Refactor / chore
- [ ] Docs
- [ ] Test / CI
- [ ] Breaking change
""",
        test_checkboxes="""
- [ ] Unit tests added / updated
- [ ] Integration tests added / updated
- [ ] E2E tests added / updated
- [ ] Manual verification completed
- [ ] Existing tests cover this change
- [ ] Not applicable
""",
    )
    result = module.validate_pr_body(body)
    assert not result.ok
    assert "Check at least one Type of change checkbox." in result.errors
    assert "Check at least one Test coverage checkbox." in result.errors


def test_rejects_missing_template_labels() -> None:
    body = _valid_body(
        type_checkboxes="""
- [x] Bug fix
""",
        test_checkboxes="""
- [x] Unit tests added / updated
""",
    )
    result = module.validate_pr_body(body)
    assert not result.ok
    assert any(error.startswith("Type of change is missing") for error in result.errors)
    assert any(error.startswith("Test coverage is missing") for error in result.errors)


def test_rejects_missing_required_heading() -> None:
    body = _valid_body().replace("## Test Plan", "## Test notes")
    result = module.validate_pr_body(body)
    assert not result.ok
    assert "Missing required section: ## Test Plan" in result.errors
    assert "Test Plan must describe how the change was tested." in result.errors


def test_rejects_placeholder_summary_and_test_plan() -> None:
    body = _valid_body(
        summary="<!-- Replace this with what changed and why. -->\nWhat changed and why?",
        test_plan="How was this change tested?",
    )
    result = module.validate_pr_body(body)
    assert not result.ok
    assert "Summary still contains template placeholder text." in result.errors
    assert "Test Plan still contains template placeholder text." in result.errors


def test_rejects_empty_summary_and_test_plan_after_html_comments() -> None:
    body = _valid_body(
        summary="<!-- Summary will be ignored because it is an HTML comment. -->",
        test_plan="<!-- Test Plan will be ignored because it is an HTML comment. -->",
    )
    result = module.validate_pr_body(body)
    assert not result.ok
    assert "Summary must describe what changed and why." in result.errors
    assert "Test Plan must describe how the change was tested." in result.errors


def test_coverage_notes_optional_for_automated_coverage() -> None:
    # Default body checks Unit + E2E and omits the Coverage notes section.
    result = module.validate_pr_body(_valid_body())
    assert result.ok, result.errors


def test_manual_verification_requires_coverage_notes() -> None:
    body = _valid_body(test_checkboxes=_MANUAL_ONLY)
    result = module.validate_pr_body(body)
    assert not result.ok
    assert any(error.startswith("Coverage notes are required") for error in result.errors)


def test_not_applicable_requires_coverage_notes() -> None:
    body = _valid_body(test_checkboxes=_NOT_APPLICABLE_ONLY)
    result = module.validate_pr_body(body)
    assert not result.ok
    assert any(error.startswith("Coverage notes are required") for error in result.errors)


def test_manual_verification_with_coverage_notes_passes() -> None:
    body = _valid_body(
        test_checkboxes=_MANUAL_ONLY,
        coverage_notes=(
            "Verified manually by running the REPL handoff flow and confirming "
            "polling resumes after a reconnect."
        ),
    )
    result = module.validate_pr_body(body)
    assert result.ok, result.errors


def test_not_applicable_with_empty_coverage_notes_after_comment_is_rejected() -> None:
    body = _valid_body(
        test_checkboxes=_NOT_APPLICABLE_ONLY,
        coverage_notes="<!-- nothing meaningful here -->",
    )
    result = module.validate_pr_body(body)
    assert not result.ok
    assert any(error.startswith("Coverage notes are required") for error in result.errors)


def test_demo_optional_for_non_ui_change() -> None:
    # Default body is a bug fix with no Demo section.
    result = module.validate_pr_body(_valid_body())
    assert result.ok, result.errors


def test_ui_change_requires_demo() -> None:
    body = _valid_body(type_checkboxes=_UI_CHANGE)
    result = module.validate_pr_body(body)
    assert not result.ok
    assert any(error.startswith("Demo is required") for error in result.errors)


def test_ui_change_with_empty_demo_after_comment_is_rejected() -> None:
    body = _valid_body(
        type_checkboxes=_UI_CHANGE,
        demo="<!-- nothing meaningful here -->",
    )
    result = module.validate_pr_body(body)
    assert not result.ok
    assert any(error.startswith("Demo is required") for error in result.errors)


def test_ui_change_with_demo_passes() -> None:
    body = _valid_body(
        type_checkboxes=_UI_CHANGE,
        demo="![new settings panel](https://github.com/org/repo/assets/1/screenshot.png)",
    )
    result = module.validate_pr_body(body)
    assert result.ok, result.errors


def test_changelog_skip_is_valid() -> None:
    result = module.validate_pr_body(_valid_body(changelog="skip"))
    assert result.ok, result.errors


def test_changelog_multiple_category_lines_valid() -> None:
    body = _valid_body(
        changelog="Added: `--watch` reruns on file changes\nFixed: REPL no longer hangs",
    )
    result = module.validate_pr_body(body)
    assert result.ok, result.errors


def test_changelog_missing_heading_rejected() -> None:
    result = module.validate_pr_body(_valid_body(changelog=None))
    assert not result.ok
    assert "Missing required section: ## Changelog" in result.errors


def test_changelog_malformed_line_rejected() -> None:
    body = _valid_body(changelog="this is just prose with no category prefix")
    result = module.validate_pr_body(body)
    assert not result.ok
    assert any(error.startswith("Changelog lines must be") for error in result.errors)


def test_changelog_unknown_category_rejected() -> None:
    body = _valid_body(changelog="Improved: faster startup")
    result = module.validate_pr_body(body)
    assert not result.ok
    assert any(error.startswith("Changelog lines must be") for error in result.errors)


def test_breaking_change_requires_changelog_entry() -> None:
    body = _valid_body(type_checkboxes=_BREAKING_TYPE, changelog="skip")
    result = module.validate_pr_body(body)
    assert not result.ok
    assert any(error.startswith("Changelog must not be 'skip'") for error in result.errors)


def test_breaking_change_with_entry_passes() -> None:
    body = _valid_body(
        type_checkboxes=_BREAKING_TYPE,
        changelog="Removed: dropped the deprecated `--legacy` flag",
    )
    result = module.validate_pr_body(body)
    assert result.ok, result.errors
