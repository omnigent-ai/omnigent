#!/usr/bin/env python3
"""Offline tests for sync_pr_priority.py."""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

SCRIPT_PATH = pathlib.Path(__file__).with_name("sync_pr_priority.py")
SPEC = importlib.util.spec_from_file_location("sync_pr_priority", SCRIPT_PATH)
sync_pr_priority = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(sync_pr_priority)


class FakeAPI:
    def __init__(self, *, closing: list[list[str]], current: list[str]) -> None:
        self._closing = closing
        self._current = current
        self.added: list[tuple[int, str]] = []
        self.removed: list[tuple[int, str]] = []

    def closing_issue_labels(self, pull_number: int) -> list[list[str]]:
        assert pull_number
        return self._closing

    def pull_labels(self, pull_number: int) -> list[str]:
        assert pull_number
        return self._current

    def add_label(self, pull_number: int, label: str) -> None:
        self.added.append((pull_number, label))

    def remove_label(self, pull_number: int, label: str) -> None:
        self.removed.append((pull_number, label))


class DesiredPriorityTest(unittest.TestCase):
    def test_no_closing_issue_yields_none(self) -> None:
        self.assertIsNone(sync_pr_priority.desired_priority([]))

    def test_closing_issue_without_priority_yields_none(self) -> None:
        self.assertIsNone(sync_pr_priority.desired_priority([["Bug", "comp:server"]]))

    def test_single_priority_is_returned(self) -> None:
        self.assertEqual(sync_pr_priority.desired_priority([["P2-medium"]]), "P2-medium")

    def test_highest_priority_wins_across_issues(self) -> None:
        self.assertEqual(
            sync_pr_priority.desired_priority([["P3-low"], ["P1-high"], ["P2-medium"]]),
            "P1-high",
        )

    def test_highest_priority_wins_within_one_issue(self) -> None:
        self.assertEqual(
            sync_pr_priority.desired_priority([["P0-critical", "P3-low"]]),
            "P0-critical",
        )


class LabelChangesTest(unittest.TestCase):
    def test_adds_missing_priority(self) -> None:
        self.assertEqual(sync_pr_priority.label_changes(["Bug"], "P1-high"), ("P1-high", []))

    def test_noop_when_already_correct(self) -> None:
        self.assertEqual(sync_pr_priority.label_changes(["P1-high", "Bug"], "P1-high"), (None, []))

    def test_replaces_stale_priority(self) -> None:
        self.assertEqual(
            sync_pr_priority.label_changes(["P3-low"], "P1-high"), ("P1-high", ["P3-low"])
        )

    def test_removes_priority_when_no_longer_desired(self) -> None:
        self.assertEqual(
            sync_pr_priority.label_changes(["P2-medium"], None), (None, ["P2-medium"])
        )

    def test_leaves_non_priority_labels_untouched(self) -> None:
        self.assertEqual(sync_pr_priority.label_changes(["Bug", "python"], None), (None, []))


class SyncPullTest(unittest.TestCase):
    def test_applies_priority_from_closing_issue(self) -> None:
        api = FakeAPI(closing=[["P1-high"]], current=["Bug"])
        sync_pr_priority.sync_pull(api, 7)
        self.assertEqual(api.added, [(7, "P1-high")])
        self.assertEqual(api.removed, [])

    def test_swaps_stale_priority(self) -> None:
        api = FakeAPI(closing=[["P0-critical"]], current=["P2-medium"])
        sync_pr_priority.sync_pull(api, 7)
        self.assertEqual(api.added, [(7, "P0-critical")])
        self.assertEqual(api.removed, [(7, "P2-medium")])

    def test_related_only_pr_gets_nothing(self) -> None:
        # No closing references -> no priority, and nothing to strip.
        api = FakeAPI(closing=[], current=["Bug"])
        sync_pr_priority.sync_pull(api, 7)
        self.assertEqual(api.added, [])
        self.assertEqual(api.removed, [])


class ClosingIssueLabelsParseTest(unittest.TestCase):
    """GraphQL response parsing tolerates null nodes and surfaces errors."""

    def _api_returning(self, response: object) -> sync_pr_priority.GitHubAPI:
        api = sync_pr_priority.GitHubAPI("token", "owner/name")

        def stub_request(*_args: object, **_kwargs: object) -> object:
            return response

        api._request = stub_request  # type: ignore[method-assign]
        return api

    def test_parses_labels(self) -> None:
        response = {
            "data": {
                "repository": {
                    "pullRequest": {
                        "closingIssuesReferences": {
                            "nodes": [{"labels": {"nodes": [{"name": "P1-high"}]}}]
                        }
                    }
                }
            }
        }
        self.assertEqual(self._api_returning(response).closing_issue_labels(1), [["P1-high"]])

    def test_null_data_yields_empty(self) -> None:
        self.assertEqual(self._api_returning({"data": None}).closing_issue_labels(1), [])

    def test_null_pull_request_yields_empty(self) -> None:
        response = {"data": {"repository": {"pullRequest": None}}}
        self.assertEqual(self._api_returning(response).closing_issue_labels(1), [])

    def test_errors_raise(self) -> None:
        response = {"data": None, "errors": [{"message": "boom"}]}
        with self.assertRaises(RuntimeError):
            self._api_returning(response).closing_issue_labels(1)


if __name__ == "__main__":
    unittest.main()
