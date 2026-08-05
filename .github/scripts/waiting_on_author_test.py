#!/usr/bin/env python3
"""Offline tests for waiting_on_author.py."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest
import urllib.error
from datetime import UTC, datetime
from email.message import Message
from typing import Any

SCRIPT_PATH = pathlib.Path(__file__).with_name("waiting_on_author.py")
SPEC = importlib.util.spec_from_file_location("waiting_on_author", SCRIPT_PATH)
waiting_on_author = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(waiting_on_author)


def pr(
    number: int = 12,
    author: str = "alice",
    labels: list[str] | None = None,
    state: str = "open",
    assignees: list[str] | None = None,
    requested_reviewers: list[str] | None = None,
) -> dict[str, Any]:
    labels = [waiting_on_author.LABEL] if labels is None else labels
    return {
        "number": number,
        "state": state,
        "user": {"login": author},
        "labels": [{"name": label} for label in labels],
        "assignees": [{"login": login} for login in (assignees or [])],
        "requested_reviewers": [{"login": login} for login in (requested_reviewers or [])],
    }


def issue(number: int, labels: list[str] | None = None, is_pr: bool = True) -> dict[str, Any]:
    labels = [waiting_on_author.LABEL] if labels is None else labels
    item: dict[str, Any] = {"number": number, "labels": [{"name": label} for label in labels]}
    if is_pr:
        item["pull_request"] = {}
    return item


def labeled_at(iso: str, label: str | None = None) -> dict[str, Any]:
    return {
        "event": "labeled",
        "label": {"name": label or waiting_on_author.LABEL},
        "created_at": iso,
    }


class FakeAPI:
    def __init__(
        self,
        *,
        pull: dict[str, Any] | None = None,
        issues: list[dict[str, Any]] | None = None,
        timeline_by_issue: dict[int, list[dict[str, Any]]] | None = None,
        issue_comments: dict[int, list[dict[str, Any]]] | None = None,
        review_comments: dict[int, list[dict[str, Any]]] | None = None,
        reviews: dict[int, list[dict[str, Any]]] | None = None,
        commits: dict[int, list[dict[str, Any]]] | None = None,
    ):
        self.pull = pull or pr()
        self.issues = issues or []
        self.timeline_by_issue = timeline_by_issue or {}
        self.issue_comments = issue_comments or {}
        self.review_comments = review_comments or {}
        self.reviews = reviews or {}
        self.commits = commits or {}
        self.removed: list[tuple[int, str]] = []
        self.closed: list[int] = []
        self.comments: list[tuple[int, str]] = []
        self.added: list[tuple[int, str]] = []
        self.review_requests: list[tuple[int, list[str]]] = []

    def get_pull(self, pull_number: int) -> dict[str, Any]:
        return self.pull | {"number": pull_number}

    def remove_label(self, issue_number: int, label: str) -> bool:
        self.removed.append((issue_number, label))
        return True

    def list_waiting_issues(self) -> list[dict[str, Any]]:
        return self.issues

    def list_timeline(self, issue_number: int) -> list[dict[str, Any]]:
        return self.timeline_by_issue.get(issue_number, [])

    def list_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        return self.issue_comments.get(issue_number, [])

    def list_review_comments(self, pull_number: int) -> list[dict[str, Any]]:
        return self.review_comments.get(pull_number, [])

    def list_reviews(self, pull_number: int) -> list[dict[str, Any]]:
        return self.reviews.get(pull_number, [])

    def list_commits(self, pull_number: int) -> list[dict[str, Any]]:
        return self.commits.get(pull_number, [])

    def add_label(self, issue_number: int, label: str) -> None:
        self.added.append((issue_number, label))

    def request_review(self, pull_number: int, reviewers: list[str]) -> int:
        self.review_requests.append((pull_number, reviewers))
        return len(reviewers)

    def close_pull(self, pull_number: int) -> None:
        self.closed.append(pull_number)

    def create_comment(self, issue_number: int, body: str) -> None:
        self.comments.append((issue_number, body))


class WaitingOnAuthorTest(unittest.TestCase):
    def test_latest_waiting_label_at_uses_latest_matching_label(self) -> None:
        self.assertEqual(
            waiting_on_author.latest_waiting_label_at(
                [
                    labeled_at("2026-07-01T00:00:00Z"),
                    labeled_at("2026-07-10T00:00:00Z", "other"),
                    labeled_at("2026-07-12T00:00:00Z"),
                ]
            ),
            "2026-07-12T00:00:00Z",
        )

    def test_author_issue_comment_removes_waiting_label(self) -> None:
        api = FakeAPI(pull=pr(author="Alice"))
        waiting_on_author.clear_on_author_activity(
            "issue_comment",
            {"issue": {"number": 12, "pull_request": {}}, "comment": {"user": {"login": "alice"}}},
            api,
        )
        self.assertEqual(api.removed, [(12, waiting_on_author.LABEL)])

    def test_author_review_thread_reply_removes_waiting_label(self) -> None:
        api = FakeAPI(pull=pr(author="alice"))
        waiting_on_author.clear_on_author_activity(
            "pull_request_review_comment",
            {"pull_request": {"number": 12}, "comment": {"user": {"login": "alice"}}},
            api,
        )
        self.assertEqual(api.removed, [(12, waiting_on_author.LABEL)])

    def test_maintainer_comment_keeps_waiting_label(self) -> None:
        api = FakeAPI(pull=pr(author="alice"))
        waiting_on_author.clear_on_author_activity(
            "issue_comment",
            {
                "issue": {"number": 12, "pull_request": {}},
                "comment": {"user": {"login": "maintainer"}},
            },
            api,
        )
        self.assertEqual(api.removed, [])

    def test_new_commits_remove_waiting_label(self) -> None:
        api = FakeAPI(pull=pr(author="alice"))
        waiting_on_author.clear_on_author_activity(
            "pull_request_target",
            {"action": "synchronize", "pull_request": {"number": 12}},
            api,
        )
        self.assertEqual(api.removed, [(12, waiting_on_author.LABEL)])

    def test_scheduled_sweep_closes_pr_after_7_days(self) -> None:
        api = FakeAPI(
            issues=[issue(20)], timeline_by_issue={20: [labeled_at("2026-07-17T00:00:00Z")]}
        )
        waiting_on_author.close_stale_waiting_prs(api, now=datetime(2026, 7, 24, tzinfo=UTC))
        self.assertEqual(api.closed, [20])
        self.assertEqual(len(api.comments), 1)
        self.assertIn(waiting_on_author.LABEL, api.comments[0][1])
        # Must point at `/reopen`, not GitHub's Reopen button: a fork author
        # cannot press that, so telling them to is advice they can't act on.
        self.assertIn("/reopen", api.comments[0][1])
        self.assertNotIn("please reopen this PR", api.comments[0][1])

    def test_scheduled_sweep_removes_label_after_author_comment(self) -> None:
        api = FakeAPI(
            pull=pr(number=23, author="alice"),
            issues=[issue(23)],
            timeline_by_issue={23: [labeled_at("2026-07-01T00:00:00Z")]},
            issue_comments={
                23: [{"user": {"login": "alice"}, "created_at": "2026-07-20T00:00:00Z"}]
            },
        )
        waiting_on_author.close_stale_waiting_prs(api, now=datetime(2026, 7, 24, tzinfo=UTC))
        self.assertEqual(api.removed, [(23, waiting_on_author.LABEL)])
        self.assertEqual(api.closed, [])

    def test_scheduled_sweep_keeps_label_after_maintainer_comment(self) -> None:
        api = FakeAPI(
            pull=pr(number=24, author="alice"),
            issues=[issue(24)],
            timeline_by_issue={24: [labeled_at("2026-07-18T00:00:00Z")]},
            issue_comments={
                24: [{"user": {"login": "maintainer"}, "created_at": "2026-07-20T00:00:00Z"}]
            },
        )
        waiting_on_author.close_stale_waiting_prs(api, now=datetime(2026, 7, 24, tzinfo=UTC))
        self.assertEqual(api.removed, [])
        self.assertEqual(api.closed, [])

    def test_scheduled_sweep_removes_label_after_new_commit(self) -> None:
        api = FakeAPI(
            pull=pr(number=25, author="alice"),
            issues=[issue(25)],
            timeline_by_issue={25: [labeled_at("2026-07-01T00:00:00Z")]},
            commits={25: [{"commit": {"author": {"date": "2026-07-20T00:00:00Z"}}}]},
        )
        waiting_on_author.close_stale_waiting_prs(api, now=datetime(2026, 7, 24, tzinfo=UTC))
        self.assertEqual(api.removed, [(25, waiting_on_author.LABEL)])
        self.assertEqual(api.closed, [])

    def test_scheduled_sweep_ignores_author_comment_before_label(self) -> None:
        api = FakeAPI(
            pull=pr(number=26, author="alice"),
            issues=[issue(26)],
            timeline_by_issue={26: [labeled_at("2026-07-17T00:00:00Z")]},
            issue_comments={
                26: [{"user": {"login": "alice"}, "created_at": "2026-07-10T00:00:00Z"}]
            },
        )
        waiting_on_author.close_stale_waiting_prs(api, now=datetime(2026, 7, 24, tzinfo=UTC))
        self.assertEqual(api.removed, [])
        self.assertEqual(api.closed, [26])

    def test_scheduled_sweep_leaves_6_day_pr_open(self) -> None:
        api = FakeAPI(
            issues=[issue(21)], timeline_by_issue={21: [labeled_at("2026-07-18T00:00:00Z")]}
        )
        waiting_on_author.close_stale_waiting_prs(api, now=datetime(2026, 7, 24, tzinfo=UTC))
        self.assertEqual(api.closed, [])
        self.assertEqual(api.comments, [])

    def test_scheduled_sweep_skips_missing_label_timestamp(self) -> None:
        api = FakeAPI(issues=[issue(22)], timeline_by_issue={22: []})
        waiting_on_author.close_stale_waiting_prs(api, now=datetime(2026, 7, 24, tzinfo=UTC))
        self.assertEqual(api.closed, [])

    def test_scheduled_sweep_caps_closures_per_run(self) -> None:
        issues = [issue(100 + idx) for idx in range(waiting_on_author.MAX_CLOSURES_PER_RUN + 3)]
        timeline = {item["number"]: [labeled_at("2026-07-01T00:00:00Z")] for item in issues}
        api = FakeAPI(issues=issues, timeline_by_issue=timeline)
        waiting_on_author.close_stale_waiting_prs(api, now=datetime(2026, 7, 24, tzinfo=UTC))
        self.assertEqual(len(api.closed), waiting_on_author.MAX_CLOSURES_PER_RUN)


class WaitingForReviewTest(unittest.TestCase):
    def test_author_reply_hands_off_to_reviewer(self) -> None:
        api = FakeAPI(pull=pr(author="alice", assignees=["maintainer1"]))
        waiting_on_author.clear_on_author_activity(
            "issue_comment",
            {"issue": {"number": 12, "pull_request": {}}, "comment": {"user": {"login": "alice"}}},
            api,
        )
        self.assertEqual(api.removed, [(12, waiting_on_author.LABEL)])
        self.assertEqual(api.added, [(12, waiting_on_author.REVIEW_LABEL)])
        # The re-request is what actually surfaces the PR in the reviewer's queue.
        self.assertEqual(api.review_requests, [(12, ["maintainer1"])])

    def test_handoff_never_requests_the_author(self) -> None:
        api = FakeAPI(pull=pr(author="alice", assignees=["alice", "maintainer1"]))
        waiting_on_author.clear_on_author_activity(
            "pull_request_target",
            {"action": "synchronize", "pull_request": {"number": 12}},
            api,
        )
        self.assertEqual(api.review_requests, [(12, ["maintainer1"])])

    def test_handoff_is_idempotent_on_the_label(self) -> None:
        api = FakeAPI(
            pull=pr(
                author="alice",
                labels=[waiting_on_author.LABEL, waiting_on_author.REVIEW_LABEL],
                assignees=["maintainer1"],
            )
        )
        waiting_on_author.clear_on_author_activity(
            "pull_request_target",
            {"action": "synchronize", "pull_request": {"number": 12}},
            api,
        )
        self.assertEqual(api.added, [], "already labeled; no duplicate add")

    def test_maintainer_comment_does_not_hand_off(self) -> None:
        api = FakeAPI(pull=pr(author="alice", assignees=["maintainer1"]))
        waiting_on_author.clear_on_author_activity(
            "issue_comment",
            {
                "issue": {"number": 12, "pull_request": {}},
                "comment": {"user": {"login": "maintainer1"}},
            },
            api,
        )
        self.assertEqual(api.added, [])
        self.assertEqual(api.review_requests, [])

    def test_labeling_waiting_on_author_clears_the_review_label(self) -> None:
        api = FakeAPI()
        handled = waiting_on_author.clear_on_author_activity(
            "pull_request_target",
            {
                "action": "labeled",
                "label": {"name": waiting_on_author.LABEL},
                "pull_request": pr(
                    labels=[waiting_on_author.LABEL, waiting_on_author.REVIEW_LABEL]
                ),
            },
            api,
        )
        self.assertTrue(handled)
        self.assertEqual(api.removed, [(12, waiting_on_author.REVIEW_LABEL)])

    def test_labeling_something_else_is_ignored(self) -> None:
        api = FakeAPI()
        handled = waiting_on_author.clear_on_author_activity(
            "pull_request_target",
            {
                "action": "labeled",
                "label": {"name": "size/M"},
                "pull_request": pr(labels=[waiting_on_author.REVIEW_LABEL]),
            },
            api,
        )
        self.assertFalse(handled)
        self.assertEqual(api.removed, [])

    def test_one_invalid_reviewer_does_not_drop_the_others(self) -> None:
        # GitHub 422s the whole batch when any login is invalid, so the request
        # has to be per-reviewer or the valid owners are silently skipped.
        posted: list[list[str]] = []

        class OneBadReviewerAPI(waiting_on_author.GitHubAPI):
            def __init__(self) -> None:
                super().__init__("token", "omnigent-ai/omnigent")

            def request(self, method: str, path: str, body: dict[str, Any] | None = None):
                assert method == "POST"
                reviewers = (body or {}).get("reviewers", [])
                posted.append(reviewers)
                if reviewers == ["gone"]:
                    raise urllib.error.HTTPError(path, 422, "not a collaborator", None, None)
                return None, Message()

        queued = OneBadReviewerAPI().request_review(12, ["gone", "maintainer1"])
        self.assertEqual(posted, [["gone"], ["maintainer1"]], "one call per reviewer")
        self.assertEqual(queued, 1, "the valid reviewer is still queued")

    def test_scheduled_sweep_hands_off_when_author_replied(self) -> None:
        api = FakeAPI(
            pull=pr(number=30, author="alice", assignees=["maintainer1"]),
            issues=[issue(30)],
            timeline_by_issue={30: [labeled_at("2026-07-01T00:00:00Z")]},
            issue_comments={
                30: [{"user": {"login": "alice"}, "created_at": "2026-07-02T00:00:00Z"}]
            },
        )
        waiting_on_author.close_stale_waiting_prs(api, now=datetime(2026, 7, 20, tzinfo=UTC))
        self.assertEqual(api.closed, [], "an author reply cancels the close")
        self.assertEqual(api.added, [(30, waiting_on_author.REVIEW_LABEL)])
        self.assertEqual(api.review_requests, [(30, ["maintainer1"])])


if __name__ == "__main__":
    unittest.main()
