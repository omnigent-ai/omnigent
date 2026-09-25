"""Tests for the shared custom-lint suppression helper (``dev/lint/_framework.py``)."""

from __future__ import annotations

from dev.lint._framework import disabled_rules_by_line


def test_disable_targets_its_own_line() -> None:
    src = "x = 1  # custom-lint: disable=workspace-scoped-cache\n"
    assert disabled_rules_by_line(src) == {1: {"workspace-scoped-cache"}}


def test_disable_next_targets_the_following_line() -> None:
    src = "# custom-lint: disable-next=workspace-scoped-cache\nx = 1\n"
    assert disabled_rules_by_line(src) == {2: {"workspace-scoped-cache"}}


def test_multiple_comma_separated_rules() -> None:
    src = "x = 1  # custom-lint: disable=rule-a, rule-b\n"
    assert disabled_rules_by_line(src) == {1: {"rule-a", "rule-b"}}


def test_trailing_reason_is_ignored() -> None:
    src = "x = 1  # custom-lint: disable=workspace-scoped-cache -- because reasons\n"
    assert disabled_rules_by_line(src) == {1: {"workspace-scoped-cache"}}


def test_marker_inside_string_literal_is_not_honored() -> None:
    # Tokenizer-based: the marker only counts inside a real comment.
    src = 'x = "# custom-lint: disable=workspace-scoped-cache"\n'
    assert disabled_rules_by_line(src) == {}


def test_no_markers_returns_empty() -> None:
    assert disabled_rules_by_line("x = 1\ny = {}\n") == {}
