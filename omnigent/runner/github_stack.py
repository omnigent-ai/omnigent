"""The pull requests in a stack, found by walking branches with ``gh pr list``."""

from __future__ import annotations

import json
import time
from collections import deque
from typing import Any

from omnigent.runner import github_resource as github
from omnigent.runner.session_prs import PullRequestRef

MAX_DEPTH = 10
MAX_GH_CALLS = 20
# One deadline for the whole walk, inside the server's 30 s wait for a read.
WALK_SECONDS = 20.0
_LIST_LIMIT = 30
_FIELDS = "number,url,title,state,isDraft,headRefName,headRefOid,baseRefName,isCrossRepository"
_UNLOADED = "GitHub could not load this pull request. Refresh to retry."


def _member(entry: dict[str, Any], *, current: bool) -> dict[str, Any]:
    return {
        "pr_url": entry.get("url"),
        "number": entry.get("number"),
        "title": entry.get("title") or "",
        "state": entry.get("state"),
        "is_draft": entry.get("isDraft") is True,
        "head_ref": entry.get("headRefName"),
        "head_sha": entry.get("headRefOid"),
        "base_ref": entry.get("baseRefName"),
        "current": current,
    }


def _list_by(
    root: str, reference: PullRequestRef, token: str | None, flag: str, branch: str
) -> tuple[list[dict[str, Any]], bool]:
    """The PRs whose ``flag`` (``--head`` or ``--base``) is ``branch``, and whether that's all."""
    rc, output, _ = github._gh(
        [
            "pr",
            "list",
            "-R",
            github._repo_argument(reference),
            flag,
            branch,
            "--state",
            "all",
            "--limit",
            str(_LIST_LIMIT),
            "--json",
            _FIELDS,
        ],
        cwd=root,
        token=token,
    )
    if rc != 0:
        return [], False
    try:
        entries = json.loads(output)
    except ValueError:
        return [], False
    if not isinstance(entries, list):
        return [], False
    found = [entry for entry in entries if isinstance(entry, dict)]
    return found, len(entries) < _LIST_LIMIT


def pull_request_stack(root: str, reference: PullRequestRef) -> dict[str, Any]:
    """The stack around the pull request: its ancestors and descendants, nearest first.

    The walk stops at ``MAX_DEPTH`` steps from the pull request, after ``MAX_GH_CALLS``
    calls or ``WALK_SECONDS``, or when it meets a pull request or head it already saw. Any
    of those, or a list that failed or was cut at its limit, marks the answer ``truncated``.
    """
    with github._gh_deadline_scope(time.monotonic() + WALK_SECONDS):
        current = github._pr_json(root, reference, _FIELDS)
        if current is None:
            raise ValueError(_UNLOADED)
        token = github._pr_token(root, reference)
        members, truncated = _walk(root, reference, token, current)
    members.append(_member(current, current=True))
    return {
        "object": "session.github.pr_stack",
        "pr_url": reference.url,
        "members": members,
        "truncated": truncated,
    }


def _walk(
    root: str, reference: PullRequestRef, token: str | None, current: dict[str, Any]
) -> tuple[list[dict[str, Any]], bool]:
    """The other members, breadth first, and whether the walk stopped before the end."""
    seen_numbers = {current.get("number")}
    seen_heads = {current.get("headRefOid")}
    members: list[dict[str, Any]] = []
    truncated = False
    calls = 0
    # Each entry is (pull request, depth, direction): "up" finds the PR whose head is its
    # base, "down" the PRs stacked on its head.
    queue: deque[tuple[dict[str, Any], int, str]] = deque(
        [(current, 0, "up"), (current, 0, "down")]
    )
    while queue:
        entry, depth, direction = queue.popleft()
        if direction == "up":
            branch, flag = entry.get("baseRefName"), "--head"
        elif entry.get("isCrossRepository") is True:
            continue  # Its head is another repository's branch, so nothing here builds on it.
        else:
            branch, flag = entry.get("headRefName"), "--base"
        if not isinstance(branch, str) or not branch:
            continue
        if depth >= MAX_DEPTH or calls >= MAX_GH_CALLS:
            return members, True
        calls += 1
        found, complete = _list_by(root, reference, token, flag, branch)
        if github._gh_timed_out.get():
            return members, True
        truncated = truncated or not complete
        for other in found:
            # Another repository's branch only shares the name of the one walked here.
            if other.get("isCrossRepository") is not False:
                continue
            number, head = other.get("number"), other.get("headRefOid")
            if number in seen_numbers or head in seen_heads:
                truncated = True
                continue
            seen_numbers.add(number)
            seen_heads.add(head)
            members.append(_member(other, current=False))
            queue.append((other, depth + 1, direction))
    return members, truncated
