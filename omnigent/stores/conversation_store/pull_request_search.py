"""Recognize a pull request reference in a session search query.

Session search is a substring match, so a search for ``#123`` would also find
``#1234`` and miss sessions that only link the PR's URL. A query naming a PR
(``#1234``, ``PR 1234``, ``!12``, or a PR URL) instead matches the sessions
that reference that exact pull request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from omnigent.git_providers import resolve_pr_url

# "#1234", "!12", "PR 1234", "pr-1234", "MR !12", "pull request #1234". A bare
# number stays a plain substring search.
_NUMBER_QUERY = re.compile(
    r"(?:(?:pr|mr|pull request|merge request)\s*[-:]?\s*)?[#!]?([1-9][0-9]{0,8})",
    re.IGNORECASE,
)
# What precedes a number in a PR reference: "#"/"!", a PR URL path segment, or
# the word "PR"/"MR". Patterns use only syntax shared by SQLite (Python re),
# PostgreSQL, CockroachDB (RE2), and MySQL (ICU).
_REFERENCE_PREFIX = (
    r"(#|!|/pull/|/pulls/|/merge_requests/|/pullrequests?/|(^|[^a-zA-Z0-9_])(pr|mr)[ :-]?)"
)
# A following digit would make it a different, longer PR number.
_NUMBER_END = r"([^0-9]|$)"
_REGEX_SPECIAL = re.compile(r"[\\^$.|?*+()\[\]{}]")


@dataclass(frozen=True)
class PullRequestSearch:
    """A search for one pull request, matched by a case-insensitive regex."""

    number: int
    pattern: str

    def matched_text(self, text: str) -> str | None:
        """
        Return the first reference to the pull request in *text*.

        :param text: Searchable item text, e.g. ``"Opened PR #1234 for review"``.
        :returns: The matched reference, e.g. ``"#1234"``, or ``None``.
        """
        match = re.search(self.pattern, text)
        return match.group(0).strip() if match else None


def parse_pull_request_query(query: str) -> PullRequestSearch | None:
    """
    Return the pull request a session search query names, if any.

    :param query: The user's search string, e.g. ``"#1234"``, ``"PR 1234"``, or
        ``"https://github.com/acme/app/pull/1234/files"``.
    :returns: A search for that pull request, or ``None`` for any other query.
        A number names any repository's PR; a URL names one repository's.
    """
    stripped = query.strip()
    number_match = _NUMBER_QUERY.fullmatch(stripped)
    if number_match is not None and not stripped[0].isdigit():
        number = int(number_match[1])
        return PullRequestSearch(number, f"(?i){_REFERENCE_PREFIX}{number}{_NUMBER_END}")
    if "://" not in stripped:
        return None
    parsed = resolve_pr_url(stripped)
    if parsed is None:
        return None
    # The canonical URL ends with the number; the scheme is left out so an
    # http:// or scheme-less mention still matches.
    location = _REGEX_SPECIAL.sub(lambda char: f"\\{char[0]}", parsed.url.split("://", 1)[1])
    return PullRequestSearch(parsed.number, f"(?i){location}{_NUMBER_END}")
