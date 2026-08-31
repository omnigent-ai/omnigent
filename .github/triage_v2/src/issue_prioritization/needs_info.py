from __future__ import annotations

import argparse
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Protocol

from issue_prioritization.comments import COMMENT_MARKER
from issue_prioritization.github import GitHubClient

CLOSE_COMMENT_MARKER = "omnigent-needs-info-expiry"
_EXEMPT_LABELS = {"duplicate", "pinned", "security"}


class NeedsInfoGitHub(Protocol):
    def open_issues_with_label(self, label: str) -> tuple[dict[str, object], ...]: ...

    def issue_data(self, issue_number: int) -> dict[str, object]: ...

    def issue_comments(self, issue_number: int) -> tuple[dict[str, object], ...]: ...

    def comment_on_issue(self, issue_number: int, body: str) -> None: ...

    def close_issue(self, issue_number: int) -> None: ...


@dataclass(frozen=True)
class ExpiredIssue:
    number: int
    deadline: date


class ExpiryBatchError(RuntimeError):
    def __init__(self, failures: tuple[tuple[int, Exception], ...]) -> None:
        self.failures = failures
        details = "; ".join(f"#{number}: {error}" for number, error in failures)
        super().__init__(f"Failed to process {len(failures)} needs-info issue(s): {details}")


def expired_issue(
    issue: Mapping[str, object],
    comments: tuple[Mapping[str, object], ...],
    today: date,
) -> ExpiredIssue | None:
    if str(issue.get("state", "")).casefold() != "open" or issue.get("pull_request"):
        return None
    labels = _labels(issue)
    if "needs-info" not in labels or "bug" not in labels or labels & _EXEMPT_LABELS:
        return None

    marker = _latest_bot_marker(comments)
    if marker is None:
        return None
    deadline = _deadline(marker)
    if deadline is None or today <= deadline:
        return None
    if _author_replied_after_marker(issue, comments, marker):
        return None
    return ExpiredIssue(int(issue["number"]), deadline)


def process_expired_issues(
    client: NeedsInfoGitHub,
    today: date,
    *,
    apply: bool,
) -> tuple[ExpiredIssue, ...]:
    expired = []
    failures = []
    for listed in client.open_issues_with_label("needs-info"):
        issue_number = int(listed["number"])
        try:
            issue = client.issue_data(issue_number)
            comments = client.issue_comments(issue_number)
            candidate = expired_issue(issue, comments, today)
            if candidate is None:
                continue
            if apply:
                # Narrow the window in which an author response or label change
                # could race with closure.
                issue = client.issue_data(issue_number)
                comments = client.issue_comments(issue_number)
                candidate = expired_issue(issue, comments, today)
                if candidate is None:
                    continue
                close_marker = _close_comment_marker(candidate.deadline)
                already_commented = any(
                    _is_bot_comment(comment)
                    and str(comment.get("body", "")).partition("\n")[0] == close_marker
                    for comment in comments
                )
                if not already_commented:
                    client.comment_on_issue(issue_number, _close_comment(candidate.deadline))
                client.close_issue(issue_number)
            expired.append(candidate)
        except Exception as error:
            failures.append((issue_number, error))
    if failures:
        raise ExpiryBatchError(tuple(failures))
    return tuple(expired)


def _labels(issue: Mapping[str, object]) -> set[str]:
    value = issue.get("labels", ())
    if not isinstance(value, (list, tuple)):
        return set()
    return {
        str(label.get("name", "")).casefold()
        for label in value
        if isinstance(label, Mapping) and label.get("name")
    }


def _latest_bot_marker(
    comments: tuple[Mapping[str, object], ...],
) -> Mapping[str, object] | None:
    candidates = [
        comment
        for comment in comments
        if COMMENT_MARKER in str(comment.get("body", "")) and _is_bot_comment(comment)
    ]
    return max(candidates, key=_comment_time, default=None)


def _is_bot_comment(comment: Mapping[str, object]) -> bool:
    user = comment.get("user")
    return isinstance(user, Mapping) and str(user.get("type", "")).casefold() == "bot"


def _deadline(comment: Mapping[str, object]) -> date | None:
    first_line = str(comment.get("body", "")).splitlines()[0]
    prefix = f"<!-- {COMMENT_MARKER} "
    if not first_line.startswith(prefix) or not first_line.endswith(" -->"):
        return None
    try:
        metadata = json.loads(first_line[len(prefix) : -4])
        if not isinstance(metadata, Mapping):
            return None
        if metadata.get("information_status") != "needs_info":
            return None
        return date.fromisoformat(str(metadata["needs_info_deadline"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _author_replied_after_marker(
    issue: Mapping[str, object],
    comments: tuple[Mapping[str, object], ...],
    marker: Mapping[str, object],
) -> bool:
    author = issue.get("user")
    author_login = str(author.get("login", "")) if isinstance(author, Mapping) else ""
    if not author_login:
        return False
    marker_time = _comment_time(marker)
    return any(
        _comment_login(comment).casefold() == author_login.casefold()
        and _comment_time(comment) > marker_time
        for comment in comments
    )


def _comment_login(comment: Mapping[str, object]) -> str:
    user = comment.get("user")
    return str(user.get("login", "")) if isinstance(user, Mapping) else ""


def _comment_time(comment: Mapping[str, object]) -> datetime:
    value = comment.get("updated_at") or comment.get("created_at")
    if not value:
        return datetime.min.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=UTC)
    return parsed.replace(tzinfo=parsed.tzinfo or UTC)


def _close_comment(deadline: date) -> str:
    return (
        f"{_close_comment_marker(deadline)}\n"
        f"Closing because the information requested by **{deadline.isoformat()}** was not added. "
        "If you can provide it later, comment here; an author follow-up will reopen and "
        "re-evaluate the issue automatically."
    )


def _close_comment_marker(deadline: date) -> str:
    metadata = json.dumps({"deadline": deadline.isoformat()}, separators=(",", ":"))
    return f"<!-- {CLOSE_COMMENT_MARKER} {metadata} -->"


def main() -> None:
    parser = argparse.ArgumentParser(description="Close expired needs-info issues")
    parser.add_argument("--github-repo", required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        raise RuntimeError("GITHUB_TOKEN is required")

    expired = process_expired_issues(
        GitHubClient(token, args.github_repo),
        datetime.now(UTC).date(),
        apply=args.apply,
    )
    mode = "closed" if args.apply else "would close"
    for issue in expired:
        print(f"Issue #{issue.number}: {mode}; needs-info deadline was {issue.deadline}")
    print(f"{len(expired)} expired needs-info issue(s); apply={args.apply}")


if __name__ == "__main__":
    main()
