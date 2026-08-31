from __future__ import annotations

import json
from collections.abc import Callable
from typing import Protocol
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

from issue_prioritization.bronze import BronzeIssue
from issue_prioritization.comments import (
    COMMENT_MARKER,
    build_triage_comment,
    preserve_needs_info_deadline,
)
from issue_prioritization.labels import LabelManifest
from issue_prioritization.mutations import (
    BotState,
    BotStateRepository,
    MutationPlan,
    MutationPlanner,
    MutationTarget,
)
from issue_prioritization.pipeline import PipelineRun


class GitHubLabels(Protocol):
    def sync_missing_labels(self, manifest: LabelManifest) -> None: ...

    def issue_labels(self, issue_number: int) -> tuple[str, ...]: ...

    def apply_labels(
        self,
        issue_number: int,
        labels_add: tuple[str, ...],
        labels_remove: tuple[str, ...],
    ) -> None: ...

    def upsert_issue_comment(self, issue_number: int, body: str) -> int: ...


class PriorityLabelHistory(Protocol):
    def priority_label_actor(self, issue_number: int, priority: str) -> str | None: ...


class GitHubClient:
    def __init__(
        self,
        token: str,
        repo: str,
        transport: Callable[[str, str, object | None], object] | None = None,
    ) -> None:
        self.token = token.strip()
        if not self.token:
            raise ValueError("GitHub token must not be empty")
        self.repo = repo
        self.transport = transport or self._request

    def sync_missing_labels(self, manifest: LabelManifest) -> None:
        existing = self._repo_labels()
        for label in manifest.labels:
            if label.name in existing:
                continue
            self.transport(
                "POST",
                "/labels",
                {
                    "name": label.name,
                    "color": label.color,
                    "description": label.description,
                },
            )

    def issue_labels(self, issue_number: int) -> tuple[str, ...]:
        value = self.issue_data(issue_number)
        labels = value.get("labels", [])
        return tuple(
            str(label["name"]) for label in labels if isinstance(label, dict) and label.get("name")
        )

    def issue_data(self, issue_number: int) -> dict[str, object]:
        value = self.transport("GET", f"/issues/{issue_number}", None)
        if not isinstance(value, dict):
            raise ValueError("GitHub issue response must be an object")
        return value

    def open_issues_with_label(self, label: str) -> tuple[dict[str, object], ...]:
        issues = []
        page = 1
        while True:
            path = f"/issues?state=open&labels={quote(label, safe='')}&per_page=100&page={page}"
            value = self.transport("GET", path, None)
            if not isinstance(value, list):
                raise ValueError("GitHub issues response must be an array")
            issues.extend(
                issue for issue in value if isinstance(issue, dict) and "pull_request" not in issue
            )
            if len(value) < 100:
                return tuple(issues)
            page += 1

    def issue_comments(self, issue_number: int) -> tuple[dict[str, object], ...]:
        comments = []
        page = 1
        while True:
            value = self.transport(
                "GET", f"/issues/{issue_number}/comments?per_page=100&page={page}", None
            )
            if not isinstance(value, list):
                raise ValueError("GitHub issue comments response must be an array")
            comments.extend(comment for comment in value if isinstance(comment, dict))
            if len(value) < 100:
                return tuple(comments)
            page += 1

    def comment_on_issue(self, issue_number: int, body: str) -> None:
        self.transport("POST", f"/issues/{issue_number}/comments", {"body": body})

    def close_issue(self, issue_number: int) -> None:
        self.transport(
            "PATCH",
            f"/issues/{issue_number}",
            {"state": "closed", "state_reason": "not_planned"},
        )

    def open_issue(self, issue_number: int) -> BronzeIssue | None:
        value = self.issue_data(issue_number)
        if value.get("state") != "open" or "pull_request" in value:
            return None
        author = value.get("user")
        author_login = str(author.get("login", "")) if isinstance(author, dict) else ""
        comments = self._author_comments(issue_number, author_login)
        if comments:
            original_body = str(value.get("body") or "")
            value = {
                **value,
                "body": "Author follow-up comments (newest first):\n\n"
                + "\n\n---\n\n".join(reversed(comments))
                + "\n\nOriginal issue body:\n\n"
                + original_body,
            }
        return BronzeIssue.from_mapping(value)

    def _author_comments(self, issue_number: int, author_login: str) -> tuple[str, ...]:
        if not author_login:
            return ()
        comments = []
        page = 1
        while True:
            value = self.transport(
                "GET", f"/issues/{issue_number}/comments?per_page=100&page={page}", None
            )
            if not isinstance(value, list):
                raise ValueError("GitHub issue comments response must be an array")
            for comment in value:
                if not isinstance(comment, dict):
                    continue
                user = comment.get("user")
                login = str(user.get("login", "")) if isinstance(user, dict) else ""
                body = str(comment.get("body") or "").strip()
                if login.casefold() == author_login.casefold() and body:
                    comments.append(body[:4000])
            if len(value) < 100:
                return tuple(comments[-5:])
            page += 1

    def apply_labels(
        self,
        issue_number: int,
        labels_add: tuple[str, ...],
        labels_remove: tuple[str, ...],
    ) -> None:
        if labels_add:
            self.transport("POST", f"/issues/{issue_number}/labels", {"labels": labels_add})
        for label in labels_remove:
            self.transport(
                "DELETE",
                f"/issues/{issue_number}/labels/{quote(label, safe='')}",
                None,
            )

    def upsert_issue_comment(self, issue_number: int, body: str) -> int:
        page = 1
        while True:
            value = self.transport(
                "GET",
                f"/issues/{issue_number}/comments?per_page=100&page={page}",
                None,
            )
            if not isinstance(value, list):
                raise ValueError("GitHub issue comments response must be an array")
            for comment in value:
                if not isinstance(comment, dict) or COMMENT_MARKER not in str(
                    comment.get("body", "")
                ):
                    continue
                comment_id = int(comment["id"])
                existing_body = str(comment.get("body", ""))
                body = preserve_needs_info_deadline(body, existing_body)
                if existing_body != body:
                    self.transport("PATCH", f"/issues/comments/{comment_id}", {"body": body})
                return comment_id
            if len(value) < 100:
                break
            page += 1
        created = self.transport("POST", f"/issues/{issue_number}/comments", {"body": body})
        if not isinstance(created, dict) or not created.get("id"):
            raise ValueError("GitHub issue comment response must include an id")
        return int(created["id"])

    def priority_label_actor(self, issue_number: int, priority: str) -> str | None:
        actor = None
        latest_event_id = -1
        page = 1
        while True:
            value = self.transport(
                "GET",
                f"/issues/{issue_number}/events?per_page=100&page={page}",
                None,
            )
            if not isinstance(value, list):
                raise ValueError("GitHub issue events response must be an array")
            for event in value:
                if not isinstance(event, dict):
                    continue
                label = event.get("label")
                if not isinstance(label, dict) or label.get("name") != priority:
                    continue
                event_id = int(event.get("id") or 0)
                if event_id < latest_event_id:
                    continue
                latest_event_id = event_id
                if event.get("event") == "unlabeled":
                    actor = None
                elif event.get("event") == "labeled":
                    event_actor = event.get("actor")
                    actor = (
                        str(event_actor["login"])
                        if isinstance(event_actor, dict) and event_actor.get("login")
                        else None
                    )
            if len(value) < 100:
                return actor
            page += 1

    def _repo_labels(self) -> set[str]:
        labels: set[str] = set()
        page = 1
        while True:
            value = self.transport("GET", f"/labels?per_page=100&page={page}", None)
            if not isinstance(value, list):
                raise ValueError("GitHub labels response must be an array")
            labels.update(
                str(label["name"])
                for label in value
                if isinstance(label, dict) and label.get("name")
            )
            if len(value) < 100:
                return labels
            page += 1

    def _request(self, method: str, path: str, payload: object | None) -> object:
        body = json.dumps(payload).encode() if payload is not None else None
        request = Request(
            f"https://api.github.com/repos/{self.repo}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urlopen(request, timeout=30) as response:
                content = response.read()
        except HTTPError as exc:
            detail = exc.read().decode(errors="replace")
            raise RuntimeError(f"GitHub API {method} {path} failed: {exc.code} {detail}") from exc
        return json.loads(content) if content else None


class GitHubLegacyPriorityOwnership:
    def __init__(self, client: PriorityLabelHistory, bot_logins: set[str]) -> None:
        self.client = client
        self.bot_logins = {login.lower() for login in bot_logins}

    def is_bot_owned(self, issue_number: int, priority: str) -> bool:
        actor = self.client.priority_label_actor(issue_number, priority)
        return actor is not None and actor.lower() in self.bot_logins


class GitHubMutationSink:
    def __init__(
        self,
        client: GitHubLabels,
        manifest: LabelManifest,
        planner: MutationPlanner,
        states: BotStateRepository,
        target_resolver: (
            Callable[[MutationTarget, tuple[str, ...], BotState | None], MutationTarget] | None
        ) = None,
    ) -> None:
        self.client = client
        self.manifest = manifest
        self.planner = planner
        self.states = states
        self.target_resolver = target_resolver

    def apply(self, run: PipelineRun) -> None:
        self.apply_with_plans(run)

    def apply_with_plans(self, run: PipelineRun) -> tuple[MutationPlan, ...]:
        self.client.sync_missing_labels(self.manifest)
        ranked = {item.issue.number: item for item in run.ranked}
        states = self.states.load()
        updated = []
        applied = []
        try:
            for proposed in run.mutations:
                issue_number = proposed.target.issue_number
                current_labels = self.client.issue_labels(issue_number)
                state = self.planner.resolve_state(
                    issue_number,
                    current_labels,
                    states.get(issue_number),
                )
                target = proposed.target
                if self.target_resolver is not None:
                    target = self.target_resolver(target, current_labels, state)
                plan = self.planner.plan_one(target, current_labels, state)
                if plan.labels_add or plan.labels_remove:
                    self.client.apply_labels(issue_number, plan.labels_add, plan.labels_remove)
                applied.append(plan)
                previous = states.get(issue_number)
                if plan.next_state != previous and (
                    previous is not None or plan.next_state.has_ownership
                ):
                    updated.append(plan.next_state)
                states[issue_number] = plan.next_state
                labels_after = _labels_after(current_labels, plan)
                if item := ranked.get(issue_number):
                    self.client.upsert_issue_comment(
                        issue_number,
                        build_triage_comment(item, plan, labels_after, run.scored_at),
                    )
        finally:
            self.states.upsert(updated)
        return tuple(applied)


def _labels_after(current: tuple[str, ...], plan: MutationPlan) -> tuple[str, ...]:
    labels = (set(current) - set(plan.labels_remove)) | set(plan.labels_add)
    return tuple(sorted(labels))
