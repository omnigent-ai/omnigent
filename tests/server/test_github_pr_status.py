"""Live PR status for a ticket's pull request pills."""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server import github_pr_status

URL = "https://github.com/example/repo/pull/7"
PR_PATH = "repos/example/repo/pulls/7"
RUNS_PATH = "repos/example/repo/commits/abc123/check-runs?per_page=100"
STATUS_PATH = "repos/example/repo/commits/abc123/status"


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    github_pr_status.clear_cache()


def _reader(responses: dict[str, Any]) -> tuple[list[str], Any]:
    calls: list[str] = []

    def fetch(path: str) -> Any:
        calls.append(path)
        response = responses.get(path)
        if response is None:
            raise OmnigentError("unreachable", code=ErrorCode.RUNNER_UNAVAILABLE)
        return response

    return calls, fetch


def _pr(**overrides: Any) -> dict[str, Any]:
    base = {"state": "open", "merged_at": None, "draft": False, "head": {"sha": "abc123"}}
    return base | overrides


def test_open_pr_buckets_check_runs_and_statuses() -> None:
    _, fetch = _reader(
        {
            PR_PATH: _pr(mergeable_state="dirty"),
            RUNS_PATH: {
                "total_count": 3,
                "check_runs": [
                    {"status": "completed", "conclusion": "success"},
                    {"status": "completed", "conclusion": "failure"},
                    {"status": "in_progress", "conclusion": None},
                ],
            },
            STATUS_PATH: {"statuses": [{"state": "error"}, {"state": "success"}]},
        }
    )
    assert github_pr_status.pull_request_status(URL, fetch=fetch) == {
        "url": URL,
        "state": "OPEN",
        "is_draft": False,
        "merge_state_status": "DIRTY",
        "checks_supported": True,
        "checks": {"failing": 2, "pending": 1, "total": 5, "partial": False},
        "additions": None,
        "deletions": None,
    }


def test_merged_pr_skips_ci_reads() -> None:
    merged = _pr(state="closed", merged_at="2026-01-01T00:00:00Z", additions=12, deletions=4)
    calls, fetch = _reader({PR_PATH: merged})
    status = github_pr_status.pull_request_status(URL, fetch=fetch)
    assert status is not None and status["state"] == "MERGED"
    assert (status["additions"], status["deletions"]) == (12, 4)
    assert calls == [PR_PATH]


def test_unreadable_ci_is_reported_as_unsupported() -> None:
    _, fetch = _reader({PR_PATH: _pr(draft=True)})
    status = github_pr_status.pull_request_status(URL, fetch=fetch)
    assert status is not None
    assert status["is_draft"] is True
    assert status["checks_supported"] is False
    assert status["checks"]["partial"] is True


def test_unreachable_github_gives_no_status_and_is_cached() -> None:
    calls, fetch = _reader({})
    clock = [100.0]
    assert github_pr_status.pull_request_status(URL, fetch=fetch, now=lambda: clock[0]) is None
    assert github_pr_status.pull_request_status(URL, fetch=fetch, now=lambda: clock[0]) is None
    assert calls == [PR_PATH]
    clock[0] += github_pr_status.CACHE_TTL_S + 1
    github_pr_status.pull_request_status(URL, fetch=fetch, now=lambda: clock[0])
    assert calls == [PR_PATH, PR_PATH]


def test_a_non_pr_url_gives_no_status() -> None:
    calls, fetch = _reader({})
    assert github_pr_status.pull_request_status("https://example.com/x", fetch=fetch) is None
    assert calls == []


def test_expired_entries_are_pruned_on_write() -> None:
    _, fetch = _reader({})
    clock = [0.0]
    github_pr_status.pull_request_status(URL, fetch=fetch, now=lambda: clock[0])
    clock[0] += github_pr_status.CACHE_TTL_S + 1
    github_pr_status.pull_request_status(
        "https://github.com/example/repo/pull/8", fetch=fetch, now=lambda: clock[0]
    )
    assert set(github_pr_status._cache) == {"https://github.com/example/repo/pull/8"}
