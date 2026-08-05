#!/usr/bin/env python3
"""Keep the waiting-on-author pull request label actionable."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from email.message import Message
from typing import Any

LABEL = "waiting-on-author"
# The other half of the cycle. `waiting-on-author` alone can only say "stalled";
# this says "back in the reviewer's queue", which is what a maintainer filters on.
REVIEW_LABEL = "waiting-for-review"
WAITING_DAYS = 7
CANONICAL_REPO = "omnigent-ai/omnigent"
MAX_CLOSURES_PER_RUN = 30


def label_names(item: dict[str, Any]) -> list[str]:
    return [
        label.get("name", label) if isinstance(label, dict) else label
        for label in item.get("labels", [])
    ]


def has_waiting_label(item: dict[str, Any]) -> bool:
    return LABEL in label_names(item)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def days_between(start: str, end: datetime) -> int:
    return int((end - parse_time(start)).total_seconds() // 86400)


def latest_waiting_label_at(timeline: list[dict[str, Any]]) -> str | None:
    latest: str | None = None
    for event in timeline:
        if event.get("event") != "labeled" or not event.get("created_at"):
            continue
        label = event.get("label") or {}
        name = label.get("name") if isinstance(label, dict) else label
        if name != LABEL:
            continue
        if latest is None or parse_time(event["created_at"]) > parse_time(latest):
            latest = event["created_at"]
    return latest


def close_message(label_applied_at: str) -> str:
    # Point at `/reopen` (reopen-pr.yml), not GitHub's Reopen button: reopening
    # needs Triage+ on the base repo, which a fork contributor does not have, so
    # telling them to reopen it themselves is advice they cannot act on.
    return "\n".join(
        [
            f"Closing this PR because it has been labeled `{LABEL}` for "
            f"{WAITING_DAYS} days without an author reply or new commit.",
            "",
            f"The label was last applied on {label_applied_at}. This isn't a "
            "judgement on the merit of the PR -- it's how we keep the review "
            "queue readable.",
            "",
            "If you're ready to continue, comment `/reopen` and this PR comes "
            "back, as long as its source branch still exists. If the branch is "
            "gone, push it again and open a fresh PR referencing this one.",
        ]
    )


class GitHubAPI:
    def __init__(self, token: str, repo: str):
        self.token = token
        self.repo = repo

    def request(
        self, method: str, path: str, body: dict[str, Any] | None = None
    ) -> tuple[Any, Message]:
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(
            f"https://api.github.com{path}",
            data=data,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        with urllib.request.urlopen(request) as response:
            raw = response.read()
            parsed = json.loads(raw.decode()) if raw else None
            return parsed, response.headers

    def paginated(self, path: str) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        next_path: str | None = path
        while next_path:
            page, headers = self.request("GET", next_path)
            items.extend(page or [])
            next_path = next_link(headers.get("Link", ""))
        return items

    def get_pull(self, pull_number: int) -> dict[str, Any]:
        pull, _ = self.request("GET", f"/repos/{self.repo}/pulls/{pull_number}")
        return pull

    def remove_label(self, issue_number: int, label: str) -> bool:
        quoted = urllib.parse.quote(label, safe="")
        try:
            self.request("DELETE", f"/repos/{self.repo}/issues/{issue_number}/labels/{quoted}")
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return False
            raise
        return True

    def list_waiting_issues(self) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode({"state": "open", "labels": LABEL, "per_page": 100})
        return self.paginated(f"/repos/{self.repo}/issues?{query}")

    def list_timeline(self, issue_number: int) -> list[dict[str, Any]]:
        return self.paginated(f"/repos/{self.repo}/issues/{issue_number}/timeline?per_page=100")

    def list_issue_comments(self, issue_number: int) -> list[dict[str, Any]]:
        return self.paginated(f"/repos/{self.repo}/issues/{issue_number}/comments?per_page=100")

    def list_review_comments(self, pull_number: int) -> list[dict[str, Any]]:
        return self.paginated(f"/repos/{self.repo}/pulls/{pull_number}/comments?per_page=100")

    def list_reviews(self, pull_number: int) -> list[dict[str, Any]]:
        return self.paginated(f"/repos/{self.repo}/pulls/{pull_number}/reviews?per_page=100")

    def list_commits(self, pull_number: int) -> list[dict[str, Any]]:
        return self.paginated(f"/repos/{self.repo}/pulls/{pull_number}/commits?per_page=100")

    def add_label(self, issue_number: int, label: str) -> None:
        self.request(
            "POST", f"/repos/{self.repo}/issues/{issue_number}/labels", {"labels": [label]}
        )

    def request_review(self, pull_number: int, reviewers: list[str]) -> int:
        """Re-request each reviewer, returning how many were queued.

        One request per reviewer: GitHub rejects the whole batch when any single
        login is invalid (a 422 for a non-collaborator), which would silently drop
        the reviewers who are still valid.
        """
        queued = 0
        for reviewer in reviewers:
            try:
                self.request(
                    "POST",
                    f"/repos/{self.repo}/pulls/{pull_number}/requested_reviewers",
                    {"reviewers": [reviewer]},
                )
                queued += 1
            except urllib.error.HTTPError as error:
                if error.code in (403, 422):
                    print(
                        f"::warning::Could not re-request @{reviewer} on "
                        f"#{pull_number}: {error.code}"
                    )
                    continue
                raise
        return queued

    def close_pull(self, pull_number: int) -> None:
        self.request("PATCH", f"/repos/{self.repo}/pulls/{pull_number}", {"state": "closed"})

    def create_comment(self, issue_number: int, body: str) -> None:
        self.request("POST", f"/repos/{self.repo}/issues/{issue_number}/comments", {"body": body})


def next_link(link_header: str) -> str | None:
    for part in link_header.split(","):
        url_part, _, rel_part = part.partition(";")
        if 'rel="next"' not in rel_part:
            continue
        url = url_part.strip()[1:-1]
        parsed = urllib.parse.urlparse(url)
        return f"{parsed.path}?{parsed.query}"
    return None


def remove_waiting_label(api: GitHubAPI, issue_number: int, reason: str) -> bool:
    removed = api.remove_label(issue_number, LABEL)
    if removed:
        print(f"Removed {LABEL} from #{issue_number}: {reason}")
    else:
        print(f"#{issue_number} no longer has {LABEL}; nothing to remove.")
    return removed


def hand_off_to_reviewer(api: GitHubAPI, pull: dict[str, Any], reason: str) -> None:
    """Move a PR from the author's court back into the reviewer's.

    The label is what maintainers filter on; the review request is what actually
    surfaces the PR in their GitHub review queue. GitHub clears the request when a
    review is submitted, so it has to be re-made here or the reply is invisible.
    """
    number = pull["number"]
    labels = label_names(pull)
    if REVIEW_LABEL not in labels:
        api.add_label(number, REVIEW_LABEL)
        print(f"Added {REVIEW_LABEL} to #{number}: {reason}")

    author = (pull.get("user") or {}).get("login", "").lower()
    # Assignees are the durable owner record; requested_reviewers empties out on
    # every submitted review. Never re-request the author's own review.
    owners = [
        login
        for login in (
            (person or {}).get("login")
            for person in (pull.get("assignees") or []) + (pull.get("requested_reviewers") or [])
        )
        if login and login.lower() != author
    ]
    queued = api.request_review(number, sorted(set(owners))) if owners else 0
    if not queued:
        # The label says "ready for a reviewer", so an empty queue makes it a lie
        # to whoever filters on it. Auto-assign normally populates assignees, so
        # this means something upstream skipped the PR.
        print(f"::warning::#{number} is {REVIEW_LABEL} with no reviewer queued")


def user_login(item: dict[str, Any]) -> str | None:
    login = item.get("user", {}).get("login")
    return login.lower() if login else None


def is_after(timestamp: str | None, since: str) -> bool:
    return bool(timestamp and parse_time(timestamp) > parse_time(since))


def authored_after(items: list[dict[str, Any]], author: str, since: str, key: str) -> bool:
    return any(user_login(item) == author and is_after(item.get(key), since) for item in items)


def commit_after(commits: list[dict[str, Any]], since: str) -> bool:
    for commit in commits:
        authored_at = commit.get("commit", {}).get("author", {}).get("date")
        committed_at = commit.get("commit", {}).get("committer", {}).get("date")
        if is_after(authored_at, since) or is_after(committed_at, since):
            return True
    return False


def author_activity_since_label(api: GitHubAPI, pull: dict[str, Any], since: str) -> str | None:
    author = pull.get("user", {}).get("login")
    if not author:
        return None
    author = author.lower()
    pull_number = pull["number"]

    if authored_after(api.list_issue_comments(pull_number), author, since, "created_at"):
        return "the author commented"
    if authored_after(api.list_review_comments(pull_number), author, since, "created_at"):
        return "the author replied to a review comment"
    if authored_after(api.list_reviews(pull_number), author, since, "submitted_at"):
        return "the author submitted a review response"
    if commit_after(api.list_commits(pull_number), since):
        return "new commits were pushed"
    return None


def clear_review_label_on_waiting(payload: dict[str, Any], api: GitHubAPI) -> bool:
    """The two labels are mutually exclusive: applying one drops the other.

    Fires when a maintainer (or the review-submitted path) sets waiting-on-author,
    so a PR never advertises both states at once.
    """
    label = (payload.get("label") or {}).get("name")
    pull = payload.get("pull_request") or {}
    if label != LABEL or not pull:
        return False
    if REVIEW_LABEL not in label_names(pull):
        return False
    removed = api.remove_label(pull["number"], REVIEW_LABEL)
    if removed:
        print(f"Removed {REVIEW_LABEL} from #{pull['number']}: now {LABEL}")
    return removed


def clear_on_author_activity(event_name: str, payload: dict[str, Any], api: GitHubAPI) -> bool:
    pull_number: int | None = None
    actor: str | None = None
    reason: str | None = None
    author_activity = False

    if event_name in {"pull_request", "pull_request_target"} and payload.get("pull_request"):
        if payload.get("action") == "labeled":
            return clear_review_label_on_waiting(payload, api)
        if payload.get("action") != "synchronize":
            return False
        pull_number = payload["pull_request"]["number"]
        reason = "new commits were pushed"
        author_activity = True
    elif event_name == "issue_comment" and "pull_request" in payload.get("issue", {}):
        pull_number = payload["issue"]["number"]
        actor = payload.get("comment", {}).get("user", {}).get("login")
        reason = "the author commented"
    elif event_name == "pull_request_review_comment" and payload.get("pull_request"):
        pull_number = payload["pull_request"]["number"]
        actor = payload.get("comment", {}).get("user", {}).get("login")
        reason = "the author replied to a review comment"
    elif event_name == "pull_request_review" and payload.get("pull_request"):
        pull_number = payload["pull_request"]["number"]
        actor = payload.get("review", {}).get("user", {}).get("login")
        reason = "the author submitted a review response"
    else:
        return False

    if pull_number is None or reason is None:
        return False
    pull = api.get_pull(pull_number)
    if pull.get("state") != "open" or not has_waiting_label(pull):
        return False

    if not author_activity:
        author = pull.get("user", {}).get("login")
        author_activity = bool(actor and author and actor.lower() == author.lower())

    if not author_activity:
        return False
    removed = remove_waiting_label(api, pull_number, reason)
    if removed:
        hand_off_to_reviewer(api, pull, reason)
    return removed


def close_stale_waiting_prs(api: GitHubAPI, now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    closed = 0
    for issue in api.list_waiting_issues():
        if closed >= MAX_CLOSURES_PER_RUN:
            break
        if "pull_request" not in issue or not has_waiting_label(issue):
            continue

        try:
            label_applied_at = latest_waiting_label_at(api.list_timeline(issue["number"]))
            if label_applied_at is None:
                print(
                    f"::warning::#{issue['number']} has {LABEL} but no label timestamp "
                    "in the timeline; skipping."
                )
                continue

            pull = api.get_pull(issue["number"])
            reason = author_activity_since_label(api, pull, label_applied_at)
            if reason:
                if remove_waiting_label(api, issue["number"], reason):
                    hand_off_to_reviewer(api, pull, reason)
                continue

            if days_between(label_applied_at, now) < WAITING_DAYS:
                continue

            api.close_pull(issue["number"])
            api.create_comment(issue["number"], close_message(label_applied_at))
            closed += 1
            print(f"Closed #{issue['number']}; {LABEL} was applied at {label_applied_at}.")
        except Exception as error:  # noqa: BLE001 - keep the sweep moving across PRs.
            print(f"::warning::Could not close #{issue['number']}: {error}")
    print(f"Closed {closed} PR(s) labeled {LABEL}.")
    return closed


def run(
    event_name: str,
    payload: dict[str, Any],
    api: GitHubAPI,
    repo: str,
    now: datetime | None = None,
) -> None:
    if repo != CANONICAL_REPO:
        print(f"Skipping {repo}; waiting-on-author hygiene only runs for {CANONICAL_REPO}.")
        return

    if event_name in {"schedule", "workflow_dispatch"}:
        close_stale_waiting_prs(api, now=now)
        return

    clear_on_author_activity(event_name, payload, api)


def load_event_payload() -> dict[str, Any]:
    path = os.environ.get("GITHUB_EVENT_PATH")
    if not path:
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def main() -> int:
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        print("GITHUB_TOKEN is required", file=sys.stderr)
        return 1
    event_name = os.environ.get("GITHUB_EVENT_NAME", "")
    run(event_name, load_event_payload(), GitHubAPI(token, repo), repo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
