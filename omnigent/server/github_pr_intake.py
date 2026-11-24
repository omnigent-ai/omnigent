"""Turn an outside GitHub pull request into a project ticket, read-only.

The server reads the PR (``gh api``, then the public REST API) and files a ticket
whose requester is ``github:<login>``. That requester puts the ticket in the
outside-author tier: landing needs the Reviewer's approval plus the owner's
``owner-ok`` for the same SHA. Nothing is ever written to GitHub.
"""

from __future__ import annotations

import json
import re
import subprocess
import urllib.request
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError

GITHUB_REQUESTER_PREFIX = "github:"
PR_LABEL = "pr"
SOURCE_LABEL = "source"
SOURCE_VALUE = "github-pr"
OWNER_OK_LABEL = "owner-ok"
_PR_URL = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/pull/(?P<number>\d+)/?$"
)
_LOGIN = re.compile(r"^[A-Za-z0-9-]{1,39}(\[bot\])?$")
_BODY_EXCERPT_CHARS = 1200
_FETCH_TIMEOUT_S = 20


def parse_pr_url(url: str) -> tuple[str, str, int]:
    """Split a GitHub PR URL into ``(owner, repo, number)``."""
    match = _PR_URL.match(url.strip())
    if match is None:
        raise OmnigentError(
            "Give a GitHub pull request URL like https://github.com/<owner>/<repo>/pull/<n>",
            code=ErrorCode.INVALID_INPUT,
        )
    return match["owner"], match["repo"], int(match["number"])


def is_outside_requester(requested_by: str | None) -> bool:
    """Whether a ticket came from an outside PR author (the stricter landing tier)."""
    return bool(requested_by) and str(requested_by).startswith(GITHUB_REQUESTER_PREFIX)


def fetch_pr(owner: str, repo: str, number: int) -> dict[str, Any]:
    """Read one PR from GitHub without writing anything."""
    path = f"repos/{owner}/{repo}/pulls/{number}"
    try:
        result = subprocess.run(
            ["gh", "api", path],
            capture_output=True,
            text=True,
            timeout=_FETCH_TIMEOUT_S,
            check=False,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        pass
    request = urllib.request.Request(
        f"https://api.github.com/{path}",
        headers={"Accept": "application/vnd.github+json", "User-Agent": "omnigent-pr-intake"},
    )
    try:
        with urllib.request.urlopen(request, timeout=_FETCH_TIMEOUT_S) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise OmnigentError(
            f"Couldn't read {owner}/{repo}#{number} from GitHub: {exc}",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        ) from exc


def _field(record: dict[str, Any], *path: str) -> Any:
    value: Any = record
    for key in path:
        value = value.get(key) if isinstance(value, dict) else None
    return value


def _quote(text: str) -> str:
    excerpt = text if len(text) <= _BODY_EXCERPT_CHARS else text[:_BODY_EXCERPT_CHARS] + "…"
    return "\n".join(f"> {line}" for line in excerpt.splitlines()) or "> (no description)"


def ticket_from_pr(url: str, record: dict[str, Any]) -> dict[str, Any]:
    """Build the ticket fields for a PR record; refuses a record for another URL."""
    owner, repo, number = parse_pr_url(url)
    if record.get("number") != number or str(record.get("html_url", "")).rstrip("/") != (
        f"https://github.com/{owner}/{repo}/pull/{number}"
    ):
        raise OmnigentError(
            "The pull request record doesn't match the URL", code=ErrorCode.INVALID_INPUT
        )
    login = str(_field(record, "user", "login") or "")
    if not _LOGIN.match(login):
        raise OmnigentError(
            "The pull request has no valid author login", code=ErrorCode.INVALID_INPUT
        )
    base = _field(record, "base", "ref") or "?"
    head_repo = _field(record, "head", "repo", "full_name") or f"{owner}/{repo}"
    head_ref = _field(record, "head", "ref") or "?"
    head_sha = _field(record, "head", "sha") or "?"
    state = record.get("state") or "?"
    branch = f"ptb/pr-{number}"
    description = (
        f"**Outside pull request** by @{login}, imported for review. State: {state}.\n\n"
        f"- PR: {url}\n"
        f"- Base `{base}`, head `{head_repo}:{head_ref}` at `{head_sha}`\n"
        f"- **Tier:** outside author. Landing needs the Reviewer's `approved` plus the owner's "
        f"`{OWNER_OK_LABEL}` set to the same SHA. Review comments stay on this ticket; "
        "nothing is posted to GitHub.\n"
        "- **Don't run its code** (tests, builds, git hooks or scripts) until the Reviewer has "
        "read the whole diff, including CI, hook and script changes.\n\n"
        f"**Checkout:** `git fetch origin pull/{number}/head:{branch}` then "
        f"`git worktree add ~/omnigent-worktrees/pr-{number} {branch}`. Fixes go on top as "
        "new commits.\n\n"
        f"**PR description:**\n{_quote(str(record.get('body') or ''))}"
    )
    title = str(record.get("title") or f"{owner}/{repo}#{number}")
    return {
        "title": f"PR #{number}: {title}"[:160],
        "description": description[:3000],
        "labels": [
            {"key": PR_LABEL, "value": f"{owner}/{repo}#{number}"},
            {"key": SOURCE_LABEL, "value": SOURCE_VALUE},
        ],
        "requested_by": f"{GITHUB_REQUESTER_PREFIX}{login}",
        "url": url,
    }
