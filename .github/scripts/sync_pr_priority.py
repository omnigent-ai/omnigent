#!/usr/bin/env python3
"""Mirror a linked issue's priority label onto the pull request that closes it.

A PR only inherits a priority when it *closes* an issue via a closing keyword
(``closes``/``fixes``/``resolves`` #n); a plain "related to #n" mention never
creates a closing link, so it is ignored. When a PR closes several issues with
different priorities the highest one wins, and stale priority labels left by an
earlier run are dropped. Pure stdlib so it runs without an install and the
label logic is unit-tested directly.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

CANONICAL_REPO = "omnigent-ai/omnigent"

# Priority labels from most to least urgent; the earliest match wins.
PRIORITY_ORDER = ("P0-critical", "P1-high", "P2-medium", "P3-low")
PRIORITY_LABELS = frozenset(PRIORITY_ORDER)

_CLOSING_ISSUES_QUERY = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      closingIssuesReferences(first: 50) {
        nodes {
          number
          labels(first: 50) { nodes { name } }
        }
      }
    }
  }
}
"""


def desired_priority(closing_issue_labels: list[list[str]]) -> str | None:
    """Highest-priority label across the issues a PR closes, or None."""
    present = {label for labels in closing_issue_labels for label in labels}
    for priority in PRIORITY_ORDER:
        if priority in present:
            return priority
    return None


def label_changes(current: list[str], desired: str | None) -> tuple[str | None, list[str]]:
    """Return the priority to add (if missing) and stale priorities to remove."""
    current_priorities = [label for label in current if label in PRIORITY_LABELS]
    to_remove = [label for label in current_priorities if label != desired]
    to_add = desired if desired is not None and desired not in current_priorities else None
    return to_add, to_remove


class GitHubAPI:
    def __init__(self, token: str, repo: str) -> None:
        self.token = token
        self.repo = repo
        self.owner, _, self.name = repo.partition("/")

    def _request(self, url: str, body: dict[str, Any] | None, method: str) -> Any:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
            return json.loads(raw.decode()) if raw else None

    def closing_issue_labels(self, pull_number: int) -> list[list[str]]:
        payload = {
            "query": _CLOSING_ISSUES_QUERY,
            "variables": {"owner": self.owner, "name": self.name, "number": pull_number},
        }
        result = self._request("https://api.github.com/graphql", payload, "POST")
        if result and result.get("errors"):
            raise RuntimeError(f"GraphQL error: {result['errors']}")
        # GitHub may return null for data or any intermediate node (e.g. an
        # unknown PR number), so treat each missing level as empty.
        data = (result or {}).get("data") or {}
        repository = data.get("repository") or {}
        pull_request = repository.get("pullRequest") or {}
        nodes = (pull_request.get("closingIssuesReferences") or {}).get("nodes") or []
        return [
            [label["name"] for label in (node.get("labels") or {}).get("nodes") or []]
            for node in nodes
        ]

    def pull_labels(self, pull_number: int) -> list[str]:
        result = self._request(
            f"https://api.github.com/repos/{self.repo}/issues/{pull_number}/labels",
            None,
            "GET",
        )
        return [label["name"] for label in result or []]

    def add_label(self, pull_number: int, label: str) -> None:
        self._request(
            f"https://api.github.com/repos/{self.repo}/issues/{pull_number}/labels",
            {"labels": [label]},
            "POST",
        )

    def remove_label(self, pull_number: int, label: str) -> None:
        quoted = urllib.parse.quote(label, safe="")
        try:
            self._request(
                f"https://api.github.com/repos/{self.repo}/issues/{pull_number}/labels/{quoted}",
                None,
                "DELETE",
            )
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise


def sync_pull(api: GitHubAPI, pull_number: int) -> None:
    desired = desired_priority(api.closing_issue_labels(pull_number))
    to_add, to_remove = label_changes(api.pull_labels(pull_number), desired)
    for label in to_remove:
        api.remove_label(pull_number, label)
        print(f"Removed stale priority {label} from #{pull_number}.")
    if to_add:
        api.add_label(pull_number, to_add)
        print(f"Applied {to_add} to #{pull_number} from its closing-linked issue(s).")
    if not to_add and not to_remove:
        print(f"#{pull_number} priority already in sync ({desired or 'none'}).")


def run(repo: str, pull_number: int, api: GitHubAPI) -> None:
    if repo != CANONICAL_REPO:
        print(f"Skipping {repo}; priority sync only runs for {CANONICAL_REPO}.")
        return
    sync_pull(api, pull_number)


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN")
    pull_number = os.environ.get("PR_NUMBER")
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 1
    if not pull_number:
        print("PR_NUMBER is required", file=sys.stderr)
        return 1
    try:
        pull_number_int = int(pull_number)
    except ValueError:
        print(f"PR_NUMBER must be an integer, got {pull_number!r}", file=sys.stderr)
        return 1
    run(repo, pull_number_int, GitHubAPI(token, repo))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
