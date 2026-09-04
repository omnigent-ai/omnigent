#!/usr/bin/env python3
"""Evaluate maintainer approval, including trusted bot authors and pushes."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

DECISIVE_REVIEW_STATES = {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}


@dataclass(frozen=True)
class ApprovalDecision:
    approved: bool
    reason: str


def latest_decisive_reviews(reviews: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return each user's latest approval-changing review."""
    latest: dict[str, tuple[tuple[str, str], dict[str, Any]]] = {}
    for review in reviews:
        user = review.get("user") or {}
        login = str(user.get("login") or "").strip()
        state = str(review.get("state") or "").upper()
        if not login or state not in DECISIVE_REVIEW_STATES:
            continue
        ordering = (str(review.get("submitted_at") or ""), str(review.get("id") or ""))
        if login not in latest or ordering >= latest[login][0]:
            latest[login] = (ordering, review)
    return {login: value[1] for login, value in latest.items()}


def approval_decision(
    *,
    repository: str,
    author: str,
    head_repository: str,
    head_sha: str,
    maintainers: set[str],
    trusted_authors: set[str],
    trusted_successors: set[str],
    reviews: list[dict[str, Any]],
    commits: list[dict[str, Any]],
    pushers: Callable[[str], set[str]],
    timeline: list[dict[str, Any]] | None = None,
) -> ApprovalDecision:
    """
    Accept trusted authors, current approval, or trusted successor pushes.

    ``pushers`` maps a commit SHA to the logins GitHub authenticated as pushing
    it. Commit author/committer logins are derived from the commit email, which
    anyone who can push to the head branch can set to the bot's address.
    """
    maintainers = {login.casefold() for login in maintainers}
    trusted_authors = {login.casefold() for login in trusted_authors}
    trusted_successors = {login.casefold() for login in trusted_successors}
    if author.casefold() in maintainers:
        return ApprovalDecision(True, f"Author @{author} is a maintainer.")
    if author.casefold() in trusted_authors:
        return ApprovalDecision(True, f"Author @{author} is trusted automation.")

    commit_positions = {
        str(commit.get("sha") or ""): index for index, commit in enumerate(commits)
    }
    failures: list[str] = []
    for login, review in latest_decisive_reviews(reviews).items():
        if login.casefold() not in maintainers:
            continue
        review_state = str(review.get("state") or "").upper()
        if review_state not in {"APPROVED", "DISMISSED"}:
            continue
        approved_sha = str(review.get("commit_id") or "")
        if review_state == "APPROVED" and approved_sha == head_sha:
            return ApprovalDecision(True, f"Current head approved by maintainer @{login}.")
        # Only the contributor can push a fork head, so nothing there is
        # attributable to trusted automation.
        if head_repository.casefold() != repository.casefold():
            failures.append(
                f"@{login}'s approval predates commits on a fork head; "
                f"a maintainer must approve {head_sha[:9]}"
            )
            continue
        position = commit_positions.get(approved_sha)
        if position is None:
            failures.append(f"@{login}'s approved commit is no longer in PR history")
            continue
        successors = commits[position + 1 :]
        if not successors or str(successors[-1].get("sha") or "") != head_sha:
            failures.append(f"@{login}'s approval does not reach the current head")
            continue
        untrusted = []
        for commit in successors:
            sha = str(commit.get("sha") or "")
            author_login = str((commit.get("author") or {}).get("login") or "").casefold()
            committer_login = str((commit.get("committer") or {}).get("login") or "").casefold()
            actors = {actor.casefold() for actor in pushers(sha)}
            if (
                author_login not in trusted_successors
                or committer_login not in trusted_successors
                or actors - trusted_successors
            ):
                untrusted.append(sha[:9])
        if untrusted:
            failures.append(
                f"@{login}'s approval predates untrusted commit(s): {', '.join(untrusted)}"
            )
            continue
        if not {actor.casefold() for actor in pushers(head_sha)} & trusted_successors:
            failures.append(
                f"@{login}'s approval predates {head_sha[:9]}, which has no push "
                "recorded from trusted automation"
            )
            continue
        if review_state == "DISMISSED" and not trusted_automatic_dismissal(
            review=review,
            successor_shas={str(commit.get("sha") or "") for commit in successors},
            trusted_successors=trusted_successors,
            timeline=timeline or [],
        ):
            failures.append(f"@{login}'s approval was not auto-dismissed by trusted automation")
            continue
        return ApprovalDecision(
            True,
            f"Maintainer @{login} approved {approved_sha[:9]}; only trusted "
            f"automation pushed and committed through {head_sha[:9]}.",
        )

    reason = "; ".join(failures) if failures else "awaiting approval from a maintainer"
    return ApprovalDecision(False, reason)


def trusted_automatic_dismissal(
    *,
    review: dict[str, Any],
    successor_shas: set[str],
    trusted_successors: set[str],
    timeline: list[dict[str, Any]],
) -> bool:
    review_id = str(review.get("id") or "")
    for event in timeline:
        dismissed = event.get("dismissed_review") or {}
        if str(event.get("event") or "") != "review_dismissed":
            continue
        if str(dismissed.get("review_id") or "") != review_id:
            continue
        if str(dismissed.get("state") or "").upper() != "APPROVED":
            continue
        if dismissed.get("dismissal_message") not in {None, ""}:
            continue
        # GitHub records the authenticated pusher as the actor of a stale-review
        # dismissal.
        if str((event.get("actor") or {}).get("login") or "").casefold() not in trusted_successors:
            continue
        if str(dismissed.get("dismissal_commit_id") or "") in successor_shas:
            return True
    return False


def gh_json(arguments: list[str]) -> Any:
    completed = subprocess.run(["gh", *arguments], check=True, capture_output=True, text=True)
    return json.loads(completed.stdout)


def paginated(endpoint: str, request: Callable[[list[str]], Any] = gh_json) -> list[dict]:
    items: list[dict] = []
    separator = "&" if "?" in endpoint else "?"
    page = 1
    while True:
        value = request(["api", f"{endpoint}{separator}per_page=100&page={page}"])
        if not isinstance(value, list):
            raise TypeError(f"expected list response, got {type(value).__name__}")
        items.extend(value)
        if len(value) < 100:
            return items
        page += 1


def pull_request_target_pushers(
    repository: str, request: Callable[[list[str]], Any] = gh_json
) -> Callable[[str], set[str]]:
    """Map a head SHA to the logins GitHub authenticated as triggering its runs."""
    cache: dict[str, set[str]] = {}

    def pushers(sha: str) -> set[str]:
        if sha not in cache:
            actors: set[str] = set()
            page = 1
            while True:
                runs = request(
                    [
                        "api",
                        f"repos/{repository}/actions/runs?head_sha={sha}"
                        f"&event=pull_request_target&per_page=100&page={page}",
                    ]
                )
                page_runs = runs.get("workflow_runs") or []
                # `actor` is the original pusher; `triggering_actor` becomes whoever
                # re-ran the check, e.g. the approval relay.
                actors |= {str((run.get("actor") or {}).get("login") or "") for run in page_runs}
                if len(page_runs) < 100:
                    break
                page += 1
            cache[sha] = actors - {""}
        return cache[sha]

    return pushers


def maintainers(repository: str) -> set[str]:
    value = gh_json(["api", f"repos/{repository}/contents/.github/MAINTAINER?ref=main"])
    content = base64.b64decode(value["content"]).decode()
    return {
        token.casefold()
        for line in content.splitlines()
        for token in line.partition("#")[0].split()
        if token
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repository", required=True)
    parser.add_argument("--pr-number", type=int, required=True)
    parser.add_argument("--trusted-author", action="append", default=[])
    parser.add_argument("--trusted-successor", action="append", default=[])
    args = parser.parse_args()

    pull = gh_json(["api", f"repos/{args.repository}/pulls/{args.pr_number}"])
    reviews = paginated(f"repos/{args.repository}/pulls/{args.pr_number}/reviews")
    commits = paginated(f"repos/{args.repository}/pulls/{args.pr_number}/commits")
    timeline = paginated(f"repos/{args.repository}/issues/{args.pr_number}/timeline")
    decision = approval_decision(
        repository=args.repository,
        author=str((pull.get("user") or {}).get("login") or ""),
        head_repository=str(((pull.get("head") or {}).get("repo") or {}).get("full_name") or ""),
        head_sha=str((pull.get("head") or {}).get("sha") or ""),
        maintainers=maintainers(args.repository),
        trusted_authors=set(args.trusted_author),
        trusted_successors=set(args.trusted_successor),
        reviews=reviews,
        commits=commits,
        pushers=pull_request_target_pushers(args.repository),
        timeline=timeline,
    )
    if decision.approved:
        print(decision.reason)
        return 0
    print(f"::error::{decision.reason}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
