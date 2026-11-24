"""Outside pull requests become outside-tier tickets from a recorded GitHub record."""

from __future__ import annotations

import pytest

from omnigent.errors import OmnigentError
from omnigent.server import github_pr_intake as intake

URL = "https://github.com/example-org/example-repo/pull/42"
RECORD = {
    "number": 42,
    "html_url": URL,
    "state": "open",
    "title": "Fix the widget",
    "body": "Makes the widget faster.",
    "user": {"login": "octocat"},
    "base": {"ref": "feat/project-ticket-board"},
    "head": {"ref": "widget", "sha": "a" * 40, "repo": {"full_name": "octocat/example-repo"}},
}


def test_a_recorded_pr_becomes_an_outside_tier_ticket() -> None:
    fields = intake.ticket_from_pr(URL, RECORD)
    assert fields["title"] == "PR #42: Fix the widget"
    assert fields["requested_by"] == "github:octocat"
    assert intake.is_outside_requester(fields["requested_by"])
    assert {"key": "pr", "value": "example-org/example-repo#42"} in fields["labels"]
    assert "git fetch origin pull/42/head:ptb/pr-42" in fields["description"]
    assert "Don't run its code" in fields["description"]
    assert not intake.is_outside_requester("member@example.com")


@pytest.mark.parametrize(
    "url, record",
    [
        ("https://example.com/not/a/pr", RECORD),
        (URL, {**RECORD, "number": 7}),
        (URL, {**RECORD, "user": {"login": "bad login!"}}),
    ],
)
def test_bad_urls_and_mismatched_records_are_refused(url: str, record: dict) -> None:
    with pytest.raises(OmnigentError):
        intake.ticket_from_pr(url, record)
