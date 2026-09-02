#!/usr/bin/env python3
"""Evaluate maintainer approval, preserving it across trusted bot commits."""

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
    trusted_successors: set[str],
    reviews: list[dict[str, Any]],
    commits: list[dict[str, Any]],
) -> ApprovalDecision:
    """Accept current approval or trusted same-repository successor commits."""
    maintainers = {login.casefold() for login in maintainers}
    trusted_successors = {login.casefold() for login in trusted_successors}
    if author.casefold() in maintainers:
        return ApprovalDecision(True, f"Author @{author} is a maintainer.")

    commit_positions = {
        str(commit.get("sha") or ""): index for index, commit in enumerate(commits)
    }
    same_repository = head_repository.casefold() == repository.casefold()
    failures: list[str] = []
    for login, review in latest_decisive_reviews(reviews).items():
        if login.casefold() not in maintainers:
            continue
        if str(review.get("state") or "").upper() != "APPROVED":
            continue
        approved_sha = str(review.get("commit_id") or "")
        if approved_sha == head_sha:
            return ApprovalDecision(True, f"Current head approved by maintainer @{login}.")
        if not same_repository:
            failures.append(f"@{login}'s approval predates a fork-head update")
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
            author_login = str((commit.get("author") or {}).get("login") or "").casefold()
            committer_login = str((commit.get("committer") or {}).get("login") or "").casefold()
            if author_login not in trusted_successors or committer_login not in trusted_successors:
                untrusted.append(str(commit.get("sha") or "")[:9])
        if not untrusted:
            return ApprovalDecision(
                True,
                f"Maintainer @{login} approved {approved_sha[:9]}; only trusted "
                f"automation committed through {head_sha[:9]}.",
            )
        failures.append(
            f"@{login}'s approval predates untrusted commit(s): {', '.join(untrusted)}"
        )

    reason = "; ".join(failures) if failures else "awaiting approval from a maintainer"
    return ApprovalDecision(False, reason)


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
    parser.add_argument("--trusted-successor", action="append", default=[])
    args = parser.parse_args()

    pull = gh_json(["api", f"repos/{args.repository}/pulls/{args.pr_number}"])
    reviews = paginated(f"repos/{args.repository}/pulls/{args.pr_number}/reviews")
    commits = paginated(f"repos/{args.repository}/pulls/{args.pr_number}/commits")
    decision = approval_decision(
        repository=args.repository,
        author=str((pull.get("user") or {}).get("login") or ""),
        head_repository=str(((pull.get("head") or {}).get("repo") or {}).get("full_name") or ""),
        head_sha=str((pull.get("head") or {}).get("sha") or ""),
        maintainers=maintainers(args.repository),
        trusted_successors=set(args.trusted_successor),
        reviews=reviews,
        commits=commits,
    )
    if decision.approved:
        print(decision.reason)
        return 0
    print(f"::error::{decision.reason}")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
