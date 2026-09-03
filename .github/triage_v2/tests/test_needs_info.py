from __future__ import annotations

import json
from datetime import date

import pytest

from issue_prioritization.comments import COMMENT_MARKER
from issue_prioritization.needs_info import (
    CLOSE_COMMENT_MARKER,
    ExpiryBatchError,
    expired_issue,
    process_expired_issues,
)


def _issue(*labels: str, state: str = "open") -> dict[str, object]:
    return {
        "number": 7,
        "state": state,
        "labels": [{"name": label} for label in labels],
        "user": {"login": "reporter"},
    }


def _marker(
    deadline: str = "2026-09-04",
    *,
    status: str = "needs_info",
    author_type: str = "Bot",
    updated_at: str = "2026-08-28T12:00:00Z",
) -> dict[str, object]:
    metadata = {
        "schema_version": 2,
        "information_status": status,
        "needs_info_deadline": deadline,
    }
    return {
        "body": f"<!-- {COMMENT_MARKER} {json.dumps(metadata)} -->\nAutomated triage",
        "user": {"login": "github-actions[bot]", "type": author_type},
        "created_at": "2026-08-28T12:00:00Z",
        "updated_at": updated_at,
    }


def test_expiry_requires_a_past_bot_managed_deadline() -> None:
    issue = _issue("Bug", "needs-info")
    scalar_marker = {
        "body": f"<!-- {COMMENT_MARKER} 42 -->",
        "user": {"login": "github-actions[bot]", "type": "Bot"},
    }

    assert expired_issue(issue, (_marker(),), date(2026, 9, 4)) is None
    assert expired_issue(issue, (_marker(),), date(2026, 9, 5)) is not None
    assert expired_issue(issue, (_marker(author_type="User"),), date(2026, 9, 5)) is None
    assert expired_issue(issue, (scalar_marker,), date(2026, 9, 5)) is None
    assert (
        expired_issue(
            issue,
            (_marker(status="sufficient"),),
            date(2026, 9, 5),
        )
        is None
    )


def test_expiry_exempts_non_bugs_security_and_pinned_issues() -> None:
    comments = (_marker(),)
    today = date(2026, 9, 5)

    assert expired_issue(_issue("Feature", "needs-info"), comments, today) is None
    assert expired_issue(_issue("Bug", "needs-info", "security"), comments, today) is None
    assert expired_issue(_issue("Bug", "needs-info", "pinned"), comments, today) is None
    assert expired_issue(_issue("Bug", "needs-info", "duplicate"), comments, today) is None


def test_expiry_waits_for_a_pending_author_response() -> None:
    author_response = {
        "body": "Here is the session ID.",
        "user": {"login": "reporter", "type": "User"},
        "created_at": "2026-09-05T01:00:00Z",
    }

    result = expired_issue(
        _issue("Bug", "needs-info"),
        (_marker(), author_response),
        date(2026, 9, 5),
    )

    assert result is None


class FakeGitHub:
    def __init__(self) -> None:
        self.issue = _issue("Bug", "needs-info")
        self.comments = [_marker()]
        self.posted = []
        self.closed = []

    def open_issues_with_label(self, label):
        assert label == "needs-info"
        return (self.issue,)

    def issue_data(self, issue_number):
        assert issue_number == 7
        return self.issue

    def issue_comments(self, issue_number):
        assert issue_number == 7
        return tuple(self.comments)

    def comment_on_issue(self, issue_number, body):
        self.posted.append((issue_number, body))

    def close_issue(self, issue_number):
        self.closed.append(issue_number)


def test_expiry_preview_is_read_only_and_apply_closes_once() -> None:
    client = FakeGitHub()

    preview = process_expired_issues(client, date(2026, 9, 5), apply=False)

    assert [item.number for item in preview] == [7]
    assert client.posted == []
    assert client.closed == []

    applied = process_expired_issues(client, date(2026, 9, 5), apply=True)

    assert applied == preview
    assert client.closed == [7]
    assert CLOSE_COMMENT_MARKER in client.posted[0][1]


def test_expiry_retry_does_not_duplicate_close_comment_after_close_failure() -> None:
    client = FakeGitHub()
    close_attempts = 0

    def close_issue(issue_number):
        nonlocal close_attempts
        close_attempts += 1
        if close_attempts == 1:
            raise RuntimeError("simulated close failure")
        client.closed.append(issue_number)

    client.close_issue = close_issue

    with pytest.raises(ExpiryBatchError, match="simulated close failure"):
        process_expired_issues(client, date(2026, 9, 5), apply=True)
    client.comments.append(
        {"body": client.posted[0][1], "user": {"login": "github-actions[bot]", "type": "Bot"}}
    )
    process_expired_issues(client, date(2026, 9, 5), apply=True)

    assert len(client.posted) == 1
    assert close_attempts == 2
    assert client.closed == [7]


def test_expiry_posts_a_new_close_comment_for_a_new_deadline() -> None:
    client = FakeGitHub()
    process_expired_issues(client, date(2026, 9, 5), apply=True)
    client.comments = [
        {"body": client.posted[0][1], "user": {"login": "github-actions[bot]", "type": "Bot"}},
        _marker(deadline="2026-09-10", updated_at="2026-09-03T12:00:00Z"),
    ]

    process_expired_issues(client, date(2026, 9, 11), apply=True)

    assert len(client.posted) == 2
    assert '"deadline":"2026-09-10"' in client.posted[1][1]
    assert client.closed == [7, 7]


def test_expiry_rechecks_live_state_before_closing() -> None:
    client = FakeGitHub()
    original_comments = client.issue_comments
    reads = 0

    def issue_comments(issue_number):
        nonlocal reads
        reads += 1
        comments = original_comments(issue_number)
        if reads == 2:
            return comments + (
                {
                    "body": "Here are the requested details.",
                    "user": {"login": "reporter", "type": "User"},
                    "created_at": "2026-09-05T01:00:00Z",
                },
            )
        return comments

    client.issue_comments = issue_comments

    assert process_expired_issues(client, date(2026, 9, 5), apply=True) == ()
    assert client.posted == []
    assert client.closed == []


def test_expiry_continues_after_one_issue_fails() -> None:
    client = FakeGitHub()
    second_issue = {**_issue("Bug", "needs-info"), "number": 8}
    client.open_issues_with_label = lambda label: (client.issue, second_issue)
    client.issue_data = lambda issue_number: client.issue if issue_number == 7 else second_issue
    client.issue_comments = lambda issue_number: tuple(client.comments)
    original_comment = client.comment_on_issue

    def comment_on_issue(issue_number, body):
        if issue_number == 7:
            raise RuntimeError("simulated API failure")
        original_comment(issue_number, body)

    client.comment_on_issue = comment_on_issue

    with pytest.raises(ExpiryBatchError, match=r"#7: simulated API failure"):
        process_expired_issues(client, date(2026, 9, 5), apply=True)

    assert client.closed == [8]
