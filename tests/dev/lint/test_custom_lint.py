"""Tests for the custom-lint aggregate runner (``dev/lint/custom_lint.py``).

One pre-commit hook runs every registered rule; the runner exits non-zero if
any rule reports a violation.
"""

from __future__ import annotations

import pytest

import dev.lint.custom_lint as custom_lint
from dev.lint.custom_lint import Rule, main


def test_workspace_scoped_cache_is_the_first_registered_rule() -> None:
    """lint_workspace_scoped_cache is registered as the first custom rule."""
    assert custom_lint.RULES, "expected at least one registered rule"
    assert custom_lint.RULES[0].name == "workspace-scoped-cache"


def test_clean_tree_passes() -> None:
    """With every rule clean on the real tree, the runner returns 0."""
    assert main() == 0


def test_any_rule_violation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """A single rule reporting a violation makes the runner return 1."""
    monkeypatch.setattr(
        custom_lint,
        "RULES",
        [Rule(name="fake", check=lambda: ["path.py:1: boom"], hint="fix it")],
    )
    assert main() == 1


def test_all_rules_clean_returns_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """When every registered rule reports no violations, the runner returns 0."""
    monkeypatch.setattr(
        custom_lint,
        "RULES",
        [Rule(name="a", check=list), Rule(name="b", check=list)],
    )
    assert main() == 0
