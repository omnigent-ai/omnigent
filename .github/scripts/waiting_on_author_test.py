#!/usr/bin/env python3
"""Offline tests for waiting_on_author.py."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest
from datetime import UTC, datetime
from typing import Any

SCRIPT_PATH = pathlib.Path(__file__).with_name("waiting_on_author.py")
SPEC = importlib.util.spec_from_file_location("waiting_on_author", SCRIPT_PATH)
waiting_on_author = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(waiting_on_author)


def pr(
    number: int = 12, author: str = "alice", labels: list[str] | None = None, state: str = "open"
) -> dict[str, Any]:
    labels = [waiting_on_author.LABEL] if labels is None else labels
    return {
        "number": number,
        "state": state,
        "user": {"login": author},
        "labels": [{"name": label} for label in labels],
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
    ):
        self.pull = pull or pr()
        self.issues = issues or []
        self.timeline_by_issue = timeline_by_issue or {}
        self.removed: list[tuple[int, str]] = []
        self.closed: list[int] = []
        self.comments: list[tuple[int, str]] = []

    def get_pull(self, pull_number: int) -> dict[str, Any]:
        return self.pull | {"number": pull_number}

    def remove_label(self, issue_number: int, label: str) -> bool:
        self.removed.append((issue_number, label))
        return True

    def list_waiting_issues(self) -> list[dict[str, Any]]:
        return self.issues

    def list_timeline(self, issue_number: int) -> list[dict[str, Any]]:
        return self.timeline_by_issue.get(issue_number, [])

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


if __name__ == "__main__":
    unittest.main()
