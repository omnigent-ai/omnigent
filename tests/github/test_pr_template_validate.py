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
    type_checkboxes: str = """
- [x] Bug fix
- [ ] Feature
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
    rationale: str = (
        "Added focused unit coverage for the cursor math and an E2E regression "
        "that exercises the REPL path."
    ),
) -> str:
    return f"""
## Summary

{summary}

## Type of change
{type_checkboxes}
## Test coverage
{test_checkboxes}
## Coverage rationale

{rationale}
"""


def test_valid_body_with_e2e_rationale() -> None:
    result = module.validate_pr_body(_valid_body())
    assert result.ok, result.errors


def test_validate_pr_body_accepts_leading_bom() -> None:
    result = module.validate_pr_body("\ufeff" + _valid_body())
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
        rationale="No tests because this is a documentation-only link update.",
    )
    result = module.validate_pr_body(body)
    assert not result.ok
    assert "Check at least one Type of change checkbox." in result.errors
    assert "Check at least one Test coverage checkbox." in result.errors


def test_requires_explanation_when_not_applicable() -> None:
    body = _valid_body(
        type_checkboxes="""
- [ ] Bug fix
- [ ] Feature
- [ ] Refactor / chore
- [x] Docs
- [ ] Test / CI
- [ ] Breaking change
""",
        test_checkboxes="""
- [ ] Unit tests added / updated
- [ ] Integration tests added / updated
- [ ] E2E tests added / updated
- [ ] Manual verification completed
- [ ] Existing tests cover this change
- [x] Not applicable
""",
        rationale="Docs only.",
    )
    result = module.validate_pr_body(body)
    assert not result.ok
    assert (
        "Not applicable test coverage requires a concrete explanation in Coverage rationale."
        in result.errors
    )


def test_rejects_missing_template_labels() -> None:
    body = _valid_body(
        type_checkboxes="""
- [x] Bug fix
""",
        test_checkboxes="""
- [x] Unit tests added / updated
""",
        rationale="Added tests for the changed parser behavior.",
    )
    result = module.validate_pr_body(body)
    assert not result.ok
    assert any(error.startswith("Type of change is missing") for error in result.errors)
    assert any(error.startswith("Test coverage is missing") for error in result.errors)


def test_rejects_missing_required_heading() -> None:
    body = _valid_body().replace("## Coverage rationale", "## Test notes")
    result = module.validate_pr_body(body)
    assert not result.ok
    assert "Missing required section: ## Coverage rationale" in result.errors
    assert (
        "Coverage rationale must explain tests run/added, or why more coverage is not needed."
        in result.errors
    )


def test_rejects_placeholder_summary_and_rationale() -> None:
    body = _valid_body(
        summary="<!-- Replace this with what changed and why. -->\nWhat changed and why?",
        rationale="Describe the exact commands you ran and coverage added.",
    )
    result = module.validate_pr_body(body)
    assert not result.ok
    assert "Summary still contains template placeholder text." in result.errors
    assert "Coverage rationale still contains template placeholder text." in result.errors


def test_rejects_empty_summary_and_rationale_after_html_comments() -> None:
    body = _valid_body(
        summary="<!-- Summary will be ignored because it is an HTML comment. -->",
        rationale="<!-- Rationale will be ignored because it is an HTML comment. -->",
    )
    result = module.validate_pr_body(body)
    assert not result.ok
    assert "Summary must describe what changed and why." in result.errors
    assert (
        "Coverage rationale must explain tests run/added, or why more coverage is not needed."
        in result.errors
    )


def test_requires_explanation_when_only_manual_coverage_selected() -> None:
    body = _valid_body(
        test_checkboxes="""
- [ ] Unit tests added / updated
- [ ] Integration tests added / updated
- [ ] E2E tests added / updated
- [x] Manual verification completed
- [ ] Existing tests cover this change
- [ ] Not applicable
""",
        rationale="Ran locally.",
    )
    result = module.validate_pr_body(body)
    assert not result.ok
    assert (
        "When no automated test coverage checkbox is selected, the rationale must explain why."
        in result.errors
    )


def test_empty_not_applicable_rationale_does_not_duplicate_short_rationale_error() -> None:
    body = _valid_body(
        test_checkboxes="""
- [ ] Unit tests added / updated
- [ ] Integration tests added / updated
- [ ] E2E tests added / updated
- [ ] Manual verification completed
- [ ] Existing tests cover this change
- [x] Not applicable
""",
        rationale="",
    )
    result = module.validate_pr_body(body)
    assert not result.ok
    assert (
        "Coverage rationale must explain tests run/added, or why more coverage is not needed."
        in result.errors
    )
    assert (
        "Not applicable test coverage requires a concrete explanation in Coverage rationale."
        not in result.errors
    )
