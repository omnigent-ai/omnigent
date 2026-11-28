"""Live state and CI of a ticket's GitHub pull requests, read-only and briefly cached.

Feeds the status pills on a ticket's pull request links. A PR GitHub won't show
(no network, rate limit, private repo) has no status; the link stays plain.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any

from omnigent.errors import OmnigentError
from omnigent.server import github_pr_intake

# A glance, not a feed; also keeps unauthenticated REST well under its hourly limit.
CACHE_TTL_S = 60.0
_FETCH_TIMEOUT_S = 6.0
_FAILED_CONCLUSIONS = frozenset({"failure", "timed_out", "cancelled", "action_required"})
_FAILED_STATES = frozenset({"failure", "error"})

_cache: dict[str, tuple[float, dict[str, Any] | None]] = {}
_lock = threading.Lock()

Fetch = Callable[[str], Any]


def _default_fetch(path: str) -> Any:
    return github_pr_intake.fetch_json(path, timeout_s=_FETCH_TIMEOUT_S)


def _checks(owner: str, repo: str, sha: str, fetch: Fetch) -> tuple[dict[str, Any], bool]:
    """Bucket the head commit's check runs and commit statuses into failing / pending / total."""
    failing = pending = total = 0
    partial = False
    read_any = False
    try:
        runs = fetch(f"repos/{owner}/{repo}/commits/{sha}/check-runs?per_page=100")
        read_any = True
        listed = runs.get("check_runs") or []
        partial = int(runs.get("total_count") or 0) > len(listed)
        for run in listed:
            total += 1
            if run.get("status") != "completed":
                pending += 1
            elif run.get("conclusion") in _FAILED_CONCLUSIONS:
                failing += 1
    except (OmnigentError, AttributeError, TypeError, ValueError):
        partial = True
    try:
        combined = fetch(f"repos/{owner}/{repo}/commits/{sha}/status")
        read_any = True
        for status in combined.get("statuses") or []:
            total += 1
            if status.get("state") == "pending":
                pending += 1
            elif status.get("state") in _FAILED_STATES:
                failing += 1
    except (OmnigentError, AttributeError, TypeError, ValueError):
        partial = True
    checks = {"failing": failing, "pending": pending, "total": total, "partial": partial}
    return checks, read_any


def _read_status(url: str, fetch: Fetch) -> dict[str, Any] | None:
    try:
        owner, repo, number = github_pr_intake.parse_pr_url(url)
        pr = fetch(f"repos/{owner}/{repo}/pulls/{number}")
    except OmnigentError:
        return None
    if not isinstance(pr, dict) or pr.get("state") not in ("open", "closed"):
        return None
    state = "MERGED" if pr.get("merged_at") else "OPEN" if pr["state"] == "open" else "CLOSED"
    sha = (pr.get("head") or {}).get("sha")
    checks, supported = (
        _checks(owner, repo, sha, fetch)
        if state == "OPEN" and isinstance(sha, str)
        else ({"failing": 0, "pending": 0, "total": 0, "partial": False}, True)
    )
    mergeable = pr.get("mergeable_state")
    return {
        "url": url,
        "state": state,
        "is_draft": bool(pr.get("draft")),
        "merge_state_status": mergeable.upper() if isinstance(mergeable, str) else None,
        "checks_supported": supported,
        "checks": checks,
        "additions": pr.get("additions") if isinstance(pr.get("additions"), int) else None,
        "deletions": pr.get("deletions") if isinstance(pr.get("deletions"), int) else None,
    }


def pull_request_status(
    url: str, *, fetch: Fetch | None = None, now: Callable[[], float] = time.monotonic
) -> dict[str, Any] | None:
    """One PR's live status, or None when GitHub can't show it. Cached for ``CACHE_TTL_S``.

    :param url: A ``https://github.com/<owner>/<repo>/pull/<n>`` URL.
    :param fetch: Reads one GitHub REST path; defaults to ``gh api``, then public REST.
    """
    with _lock:
        cached = _cache.get(url)
        if cached is not None and cached[0] > now():
            return cached[1]
    status = _read_status(url, fetch or _default_fetch)
    with _lock:
        current = now()
        # Drop expired entries so the cache only holds PRs viewed in the last minute.
        for stale in [key for key, (expires, _) in _cache.items() if expires <= current]:
            del _cache[stale]
        # Failures are cached too, so an unreachable GitHub isn't retried on every view.
        _cache[url] = (current + CACHE_TTL_S, status)
    return status


def clear_cache() -> None:
    """Forget cached statuses (tests)."""
    with _lock:
        _cache.clear()
