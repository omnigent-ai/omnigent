"""Tests for recognizing pull request references in session search queries."""

from __future__ import annotations

import pytest

from omnigent.stores.conversation_store.pull_request_search import parse_pull_request_query


@pytest.mark.parametrize(
    "query",
    ["#1234", " PR 1234 ", "pr#1234", "PR-1234", "pull request #1234", "MR !1234", "!1234"],
)
def test_number_queries_name_a_pull_request(query: str) -> None:
    """Each common way of writing a PR number names that PR."""
    search = parse_pull_request_query(query)
    assert search is not None
    assert search.number == 1234


@pytest.mark.parametrize(
    "query",
    ["1234", "#", "#0", "#12a", "PR", "deploy 1234", "https://example.com/acme/app"],
)
def test_other_queries_stay_substring_searches(query: str) -> None:
    """A bare number, plain text, or a non-PR URL is not a PR search."""
    assert parse_pull_request_query(query) is None


@pytest.mark.parametrize(
    ("text", "reference"),
    [
        ("Created https://github.com/acme/app/pull/1234\n", "/pull/1234"),
        ("Merged pull request #1234 (fix login)", "#1234"),
        ("PR 1234 is green", "PR 1234"),
        ("https://gitlab.com/group/app/-/merge_requests/1234", "/merge_requests/1234"),
        ("https://dev.azure.com/o/p/_git/r/pullrequest/1234", "/pullrequest/1234"),
        ('{"url": "https://api.github.com/repos/acme/app/pulls/1234"}', "/pulls/1234"),
    ],
)
def test_number_search_finds_references(text: str, reference: str) -> None:
    """A number search matches links and mentions of that PR in any repository."""
    search = parse_pull_request_query("#1234")
    assert search is not None
    matched = search.matched_text(text)
    assert matched is not None
    assert reference in matched


@pytest.mark.parametrize(
    "text",
    [
        "https://github.com/acme/app/pull/12345",
        "https://github.com/acme/app/pull/91234",
        "see #12345",
        "listening on port 1234",
        "spr1234",
    ],
)
def test_number_search_ignores_other_numbers(text: str) -> None:
    """A longer PR number or an unrelated number is not a reference to the PR."""
    search = parse_pull_request_query("#1234")
    assert search is not None
    assert search.matched_text(text) is None


def test_url_search_matches_only_that_repository() -> None:
    """A PR URL query, with a tab suffix and any letter case, names one repository's PR."""
    search = parse_pull_request_query("https://github.com/Acme/App/pull/1234/files")
    assert search is not None
    assert search.number == 1234
    assert search.matched_text("Opened https://github.com/acme/app/pull/1234.") is not None
    assert search.matched_text("https://github.com/other/app/pull/1234") is None
    assert search.matched_text("https://github.com/acme/app/pull/12345") is None
    assert search.matched_text("Merged #1234") is None
