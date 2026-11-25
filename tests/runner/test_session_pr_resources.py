"""Explicit PR selection cannot leak back to the session's current checkout."""

from __future__ import annotations

import asyncio
import base64
import functools
import json
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from omnigent.runner import create_runner_app, pr_resource
from omnigent.runner import github_resource as github
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
from omnigent.runner.slow_io import run_slow_io
from omnigent.workspace_fs import WorkspaceReader
from tests._helpers.polling import poll_until, poll_until_async
from tests.budgets import budget
from tests.runner.helpers import NullServerClient

A = "https://github.com/example/one/pull/42"
B = "https://github.com/example/two/pull/42"


@pytest.fixture
def tracked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(github.shutil, "which", lambda _: "/bin/gh")
    monkeypatch.setattr(github._config, "github_account_preference", lambda _: None)
    monkeypatch.setattr(github, "_list_accounts", lambda _: (True, []))
    SessionPrRegistry("session").record(
        [PullRequestRef.from_url(A), PullRequestRef.from_url(B)],
        relationship="created",
        source="test",
    )
    return str(tmp_path)


def test_explicit_repo_is_used_for_all_pr_reads(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        calls.append(args)
        if args[:2] == ["pr", "view"]:
            return 0, json.dumps({"number": 42, "title": "Second repository", "state": "OPEN"}), ""
        if args[:2] == ["pr", "diff"]:
            return 0, "second-repo-patch", ""
        return 0, json.dumps([{"filename": "second.py", "status": "added"}]), ""

    def forbidden_git(*_args: object, **_kwargs: object) -> None:
        pytest.fail("Tracked PRs must not depend on local git")

    monkeypatch.setattr(github, "_gh", gh)
    monkeypatch.setattr(github, "_git", forbidden_git)
    monkeypatch.setattr(pr_resource, "_git_output", forbidden_git)
    info = github.github_info(tracked, session_id="session", pr_url=B)
    assert info["pr"]["title"] == "Second repository"
    # Explicit PR reads omit the workspace repo.
    assert "workspace_repo" not in info
    assert {pr["url"] for pr in info["prs"]} == {A, B}
    assert (
        github.github_changed_files(tracked, session_id="session", pr_url=B)["data"][0]["path"]
        == "second.py"
    )
    assert (
        github.github_pr_diff(tracked, session_id="session", pr_url=B)["patch"]
        == "second-repo-patch"
    )
    assert calls[0][calls[0].index("-R") + 1] == "github.com/example/two"
    # The title refresh reads the state in the same call.
    assert calls[1] == [
        "pr",
        "view",
        "42",
        "-R",
        "github.com/example/one",
        "--json",
        github._PR_STATUS_FIELDS,
    ]
    assert calls[2][-1] == "repos/example/two/pulls/42/files?per_page=100"
    assert calls[3][-2:] == ["-R", "github.com/example/two"]


def test_titles_include_unselected_prs_and_are_cached_between_polls(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[list[str]] = []
    titles = {"github.com/example/one": " First repository ", "github.com/example/two": "Second"}

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        calls.append(args)
        return 0, json.dumps({"title": titles[args[args.index("-R") + 1]]}), ""

    monkeypatch.setattr(github, "_gh", gh)
    info = github.github_info(tracked, session_id="session", pr_url=B)
    assert {pr["url"]: pr["title"] for pr in info["prs"]} == {
        A: "First repository",
        B: "Second",
    }
    assert [call[-1] for call in calls] == [
        github._PR_VIEW_FIELDS + ",headRefOid,baseRefOid,headRepository,headRepositoryOwner",
        github._PR_STATUS_FIELDS,
    ]
    registry = SessionPrRegistry("session")
    assert {pr.url: pr.title for pr in registry.list()} == {A: "First repository", B: "Second"}
    before = registry.path.read_bytes()
    github.github_info(tracked, session_id="session", pr_url=B)
    assert len(calls) == 3
    assert registry.path.read_bytes() == before

    titles["github.com/example/two"] = "Renamed second PR"
    info = github.github_info(tracked, session_id="session", pr_url=B)
    assert {pr["url"]: pr["title"] for pr in info["prs"]}[B] == "Renamed second PR"
    assert {pr.url: pr.title for pr in registry.list()}[B] == "Renamed second PR"
    assert len(calls) == 4


def test_a_title_refresh_reports_each_unselected_prs_state(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The server stores every listed PR's state; only PRs looked up now carry one."""

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        if args[-1] == github._PR_STATUS_FIELDS:
            return 0, json.dumps({"title": "First", "state": "MERGED", "isDraft": False}), ""
        return 0, json.dumps({"number": 42, "title": "Second", "state": "OPEN"}), ""

    monkeypatch.setattr(github, "_gh", gh)
    info = github.github_info(tracked, session_id="session", pr_url=B)
    listed = {pr["url"]: pr for pr in info["prs"]}
    assert (listed[A]["title"], listed[A]["state"], listed[A]["is_draft"]) == (
        "First",
        "MERGED",
        False,
    )
    # The selected PR's state is the full read's, outside its listed entry.
    assert "state" not in listed[B]
    # Cached titles are not looked up again, so they carry no state.
    info = github.github_info(tracked, session_id="session", pr_url=B)
    assert all("state" not in pr for pr in info["prs"])


def test_a_provider_without_status_lookups_reports_titles_alone(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class TitleOnly:
        def titles_available(self, _root: str) -> bool:
            return True

        def pr_title(self, _root: str, _reference: object, _deadline: float) -> tuple[str, bool]:
            return "Title only", False

    monkeypatch.setattr(pr_resource, "_facet", lambda _provider: TitleOnly())
    registry = SessionPrRegistry("session")
    rows = pr_resource._session_prs_with_titles(
        tracked, {}, registry, registry.list(), time.monotonic() + 5
    )
    assert [(row["title"], "state" in row) for row in rows] == [("Title only", False)] * 2


def test_titles_refresh_after_cache_expiry_and_keep_last_known_on_failure(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = SessionPrRegistry("session")
    registry.update_titles(
        {A: "First", B: "Second"},
        timestamp=time.time() - pr_resource._PR_TITLE_CACHE_SECONDS - 1,
    )
    calls: list[list[str]] = []

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        calls.append(args)
        return 1, "", "not authenticated"

    monkeypatch.setattr(github, "_gh", gh)
    info = github.github_info(tracked, session_id="session", pr_url=B)
    assert info["pr"] is None
    assert {pr["url"]: pr["title"] for pr in info["prs"]} == {A: "First", B: "Second"}
    assert len(calls) == 2
    assert all(pr.title_checked_at > time.time() - 10 for pr in registry.list())
    github.github_info(tracked, session_id="session", pr_url=B)
    assert len(calls) == 3


@pytest.mark.parametrize("title", [None, "", " \n ", 42, {}])
def test_invalid_titles_fall_back_and_failed_lookups_are_cached(
    tracked: str, monkeypatch: pytest.MonkeyPatch, title: object
) -> None:
    calls: list[list[str]] = []

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        calls.append(args)
        return 0, json.dumps({"title": title}), ""

    monkeypatch.setattr(github, "_gh", gh)
    info = github.github_info(tracked, session_id="session", pr_url=B)
    assert all(pr["title"] is None for pr in info["prs"])
    assert len(calls) == 2
    github.github_info(tracked, session_id="session", pr_url=B)
    assert len(calls) == 3


def test_title_lookups_are_skipped_without_gh(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(github.shutil, "which", lambda _: None)
    monkeypatch.setattr(github, "_pr_json", lambda *_a: pytest.fail("gh is unavailable"))
    registry = SessionPrRegistry("session")
    registry.update_titles({A: "First"}, timestamp=1)
    before = registry.path.read_bytes()
    info = github.github_info(tracked, session_id="session", pr_url=B)
    assert {pr["url"]: pr["title"] for pr in info["prs"]} == {A: "First", B: None}
    assert registry.path.read_bytes() == before


@pytest.mark.parametrize(
    "error", [OSError("read-only"), ValueError("invalid"), FileLockTimeout("lock")]
)
def test_title_cache_failure_does_not_break_metadata(
    tracked: str, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    monkeypatch.setattr(github, "_gh", lambda *_a, **_kw: (0, '{"title": "PR title"}', ""))

    def fail(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(SessionPrRegistry, "update_titles", fail)
    info = github.github_info(tracked, session_id="session", pr_url=B)
    assert info["pr"]["title"] == "PR title"
    assert all(pr["title"] == "PR title" for pr in info["prs"])


def test_branch_inference_reuses_selected_title(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        github,
        "_workspace_github_info",
        lambda _: {"available": True, "gh_available": True, "pr": {"url": A, "title": "Inferred"}},
    )
    monkeypatch.setattr(github, "_workspace_key", lambda _: None)
    monkeypatch.setattr(github, "_pr_json", lambda *_a: pytest.fail("title is already fetched"))
    info = github.github_info(tracked, session_id="untracked")
    assert info["prs"][0]["title"] == "Inferred"
    assert SessionPrRegistry("untracked").list()[0].title == "Inferred"


def test_slow_title_lookups_preserve_metadata_and_leave_queued_prs_for_next_poll(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = 10.0
    fake_time = SimpleNamespace(monotonic=lambda: clock, time=time.time)
    monkeypatch.setattr(github, "time", fake_time)
    monkeypatch.setattr(pr_resource, "time", fake_time)
    registry = SessionPrRegistry("session")
    registry.record(
        [PullRequestRef.from_url(A.replace("42", str(number))) for number in range(43, 49)],
        relationship="created",
        source="test",
    )
    monkeypatch.setattr(pr_resource, "_PR_TITLE_LOOKUP_SECONDS", 0.2)
    attempted: list[str] = []
    title_lookups = Barrier(4, timeout=budget(5))

    def run(argv: list[str], *, timeout: float, **_kwargs: object) -> tuple[int | None, str, str]:
        nonlocal clock
        if argv[-1] != github._PR_STATUS_FIELDS:
            assert github._gh_deadline.get() is None
            assert timeout == github._gh_timeout_seconds()
            return 0, '{"title": "Selected PR"}', ""
        assert 0 < timeout <= 0.2
        attempted.append(argv[3])
        deadline = github._gh_deadline.get()
        assert deadline is not None
        title_lookups.wait()
        # Concurrent timeouts expire at the same deadline, regardless of scheduling.
        clock = deadline
        return None, "", "timed out"

    monkeypatch.setattr(github, "_run", run)
    started = clock
    info = github.github_info(tracked, session_id="session", pr_url=B)
    assert clock - started == pytest.approx(0.2)
    assert info["pr"]["title"] == "Selected PR"
    assert len(attempted) == 4
    skipped = {entry.url for entry in registry.list() if entry.title_checked_at == 0}
    assert len(skipped) == 7 - len(attempted)

    monkeypatch.setattr(github, "_run", lambda *_a, **_kw: (0, '{"title": "Fetched"}', ""))
    info = github.github_info(tracked, session_id="session", pr_url=B)
    assert all(pr["title"] == "Fetched" for pr in info["prs"] if pr["url"] in skipped)


def test_title_retries_do_not_starve_queued_prs_after_backoff_expires(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = 10.0
    wall_clock = 1000.0
    monkeypatch.setattr(github.time, "monotonic", lambda: clock)
    monkeypatch.setattr(github.time, "time", lambda: wall_clock)
    registry = SessionPrRegistry("session")
    registry.record(
        [PullRequestRef.from_url(A.replace("42", str(number))) for number in range(43, 49)],
        relationship="created",
        source="test",
    )
    original_order = [entry.url for entry in registry.list()]
    pending_numbers = {str(entry.number) for entry in registry.list() if entry.url != B}
    attempted: list[str] = []

    def run(argv: list[str], *, timeout: float, **_kwargs: object) -> tuple[int | None, str, str]:
        nonlocal clock
        if argv[-1] != github._PR_STATUS_FIELDS:
            return 0, '{"title": "Selected PR"}', ""
        attempted.append(argv[3])
        clock += timeout
        return None, "", "timed out"

    monkeypatch.setattr(github, "_run", run)
    seen: set[str] = set()
    for _ in range(len(pending_numbers)):
        before = len(attempted)
        info = github.github_info(tracked, session_id="session", pr_url=B)
        batch = set(attempted[before:])
        assert batch and batch.isdisjoint(seen)
        seen.update(batch)
        assert [entry["url"] for entry in info["prs"]] == original_order
        assert all(
            entry.title_lookup_timed_out and entry.title_checked_at == wall_clock
            for entry in registry.list()
            if entry.url != B and str(entry.number) in batch
        )
        if seen == pending_numbers:
            break
        wall_clock += pr_resource._PR_TITLE_TIMEOUT_RETRY_SECONDS + 1
    assert seen == pending_numbers


@pytest.mark.parametrize("command_seconds", [0.6, 3])
def test_title_deadline_is_shared_across_enterprise_auth_and_view(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command_seconds: float
) -> None:
    clock = 10.0
    timeouts: list[float] = []
    monkeypatch.setattr(github.time, "monotonic", lambda: clock)
    monkeypatch.setattr(github, "_in_sandbox", lambda: False)
    monkeypatch.setattr(github._config, "github_account_preference", lambda _: "work")

    def run(argv: list[str], *, timeout: float, **_kwargs: object) -> tuple[int, str, str]:
        nonlocal clock
        timeouts.append(timeout)
        clock += command_seconds
        if argv[1:3] == ["auth", "status"]:
            return 0, '{"hosts": {"github.example.org": [{"login": "work"}]}}', ""
        if argv[1:3] == ["auth", "token"]:
            return 0, "test-token", ""
        return 0, '{"title": "Enterprise PR"}', ""

    monkeypatch.setattr(github, "_run", run)
    token = github._gh_deadline.set(12.0)
    timeout_token = github._gh_timed_out.set(False)
    try:
        result = github._pr_json(
            str(tmp_path),
            PullRequestRef.from_url(A.replace("github.com", "github.example.org")),
            "title",
        )
        timed_out = github._gh_timed_out.get()
    finally:
        github._gh_timed_out.reset(timeout_token)
        github._gh_deadline.reset(token)
    if command_seconds == 0.6:
        assert timeouts == pytest.approx([2.0, 1.4, 0.8])
        assert result == {"title": "Enterprise PR"}
        assert timed_out is False
    else:
        assert timeouts == [2.0]
        assert result is None
        assert timed_out is True
    assert github._gh_deadline.get() is None


def test_slow_selected_metadata_does_not_get_extra_title_work(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = 10.0
    monkeypatch.setattr(github.time, "monotonic", lambda: clock)

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        nonlocal clock
        assert args[-1] != github._PR_STATUS_FIELDS
        clock += pr_resource._PR_TITLE_REQUEST_SECONDS + 0.1
        return 0, '{"title": "Selected PR"}', ""

    monkeypatch.setattr(github, "_gh", gh)
    info = github.github_info(tracked, session_id="session", pr_url=B)
    assert info["pr"]["title"] == "Selected PR"
    assert {pr["url"]: pr["title"] for pr in info["prs"]} == {A: None, B: "Selected PR"}
    assert all(entry.title_checked_at == 0 for entry in SessionPrRegistry("session").list())


def test_title_timeout_at_request_deadline_retries_after_short_backoff(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = 10.0
    wall_clock = 1000.0
    timeouts: list[float] = []
    monkeypatch.setattr(github.time, "monotonic", lambda: clock)
    monkeypatch.setattr(github.time, "time", lambda: wall_clock)
    registry = SessionPrRegistry("session")
    registry.update_titles({A: "Last known title"}, timestamp=wall_clock - 301)

    def run(argv: list[str], *, timeout: float, **_kwargs: object) -> tuple[int | None, str, str]:
        nonlocal clock
        if argv[-1] != github._PR_STATUS_FIELDS:
            clock += 6.5
            return 0, '{"title": "Selected PR"}', ""
        timeouts.append(timeout)
        if len(timeouts) == 1:
            clock += timeout
            return None, "", "timed out"
        return 0, '{"title": "Recovered title"}', ""

    monkeypatch.setattr(github, "_run", run)
    info = github.github_info(tracked, session_id="session", pr_url=B)
    assert timeouts == [1.5]
    assert {entry["url"]: entry["title"] for entry in info["prs"]}[A] == "Last known title"
    entry = next(entry for entry in registry.list() if entry.url == A)
    assert entry.title_checked_at == wall_clock
    assert entry.title_lookup_timed_out
    assert github._gh_timed_out.get() is False

    wall_clock += pr_resource._PR_TITLE_TIMEOUT_RETRY_SECONDS - 0.1
    github.github_info(tracked, session_id="session", pr_url=B)
    assert timeouts == [1.5]

    wall_clock += 0.1
    info = github.github_info(tracked, session_id="session", pr_url=B)
    assert timeouts == [1.5, 1.5]
    assert {entry["url"]: entry["title"] for entry in info["prs"]}[A] == "Recovered title"
    entry = next(entry for entry in registry.list() if entry.url == A)
    assert entry.title_checked_at == wall_clock
    assert entry.title_lookup_timed_out is False
    wall_clock += pr_resource._PR_TITLE_TIMEOUT_RETRY_SECONDS
    github.github_info(tracked, session_id="session", pr_url=B)
    assert timeouts == [1.5, 1.5]


@pytest.mark.parametrize(
    ("returncode", "error", "command_seconds"),
    [(1, "not found", 2.0), (None, "spawn failed", 2.0), (None, "timed out", 0.5)],
)
def test_non_deadline_failures_keep_normal_title_cache(
    tracked: str,
    monkeypatch: pytest.MonkeyPatch,
    returncode: int | None,
    error: str,
    command_seconds: float,
) -> None:
    clock = 10.0
    wall_clock = 1000.0
    attempted = 0
    monkeypatch.setattr(github.time, "monotonic", lambda: clock)
    monkeypatch.setattr(github.time, "time", lambda: wall_clock)
    monkeypatch.setattr(github, "_gh_timeout_seconds", lambda: command_seconds)

    def run(argv: list[str], **_kwargs: object) -> tuple[int | None, str, str]:
        nonlocal clock, attempted
        if argv[-1] != github._PR_STATUS_FIELDS:
            return 0, '{"title": "Selected PR"}', ""
        attempted += 1
        clock += command_seconds
        return returncode, "", error

    monkeypatch.setattr(github, "_run", run)
    github.github_info(tracked, session_id="session", pr_url=B)
    entry = next(entry for entry in SessionPrRegistry("session").list() if entry.url == A)
    assert entry.title_checked_at == wall_clock
    assert entry.title_lookup_timed_out is False
    for elapsed in [
        pr_resource._PR_TITLE_TIMEOUT_RETRY_SECONDS,
        pr_resource._PR_TITLE_CACHE_SECONDS - 1,
    ]:
        wall_clock = 1000.0 + elapsed
        github.github_info(tracked, session_id="session", pr_url=B)
        assert attempted == 1
    wall_clock = 1000.0 + pr_resource._PR_TITLE_CACHE_SECONDS
    github.github_info(tracked, session_id="session", pr_url=B)
    assert attempted == 2


def test_unassociated_selection_is_rejected(tracked: str) -> None:
    with pytest.raises(ValueError, match="not associated"):
        github.github_pr_diff(tracked, session_id="session", pr_url=A.replace("42", "99"))


def test_auth_failure_preserves_pr_list(tracked: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "_gh", lambda *_args, **_kwargs: (1, "", "not authenticated"))
    accounts = [{"login": "alice", "host": "github.com", "active": True}]
    monkeypatch.setattr(github, "_list_accounts", lambda _: (True, accounts))
    monkeypatch.setattr(github._config, "github_account_preference", lambda _: "alice")
    info = github.github_info(tracked, session_id="session", pr_url=B)
    assert info["accounts"] == accounts
    assert info["selected_account"] == "alice"
    assert info["authenticated"] is False
    assert info["selected_pr_url"] == B
    assert len(info["prs"]) == 2


def test_context_uses_fork_head_and_merge_base(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoints: list[str] = []

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        endpoint = args[-1]
        endpoints.append(endpoint)
        if endpoint.endswith("/pulls/42"):
            value = {
                "head": {"sha": "head123", "repo": {"full_name": "fork/two"}},
                "base": {"sha": "base123"},
            }
        elif "/compare/" in endpoint:
            value = {"merge_base_commit": {"sha": "merge123"}}
        else:
            value = {"encoding": "base64", "content": base64.b64encode(endpoint.encode()).decode()}
        return 0, json.dumps(value), ""

    monkeypatch.setattr(github, "_gh", gh)
    result = github.github_file_diff(
        tracked,
        "main",
        "new.py",
        session_id="session",
        pr_url=B,
        previous_path="old.py",
        head_sha="head123",
        base_sha="base123",
    )
    assert result["before"] == "repos/example/two/contents/old.py?ref=merge123"
    assert result["after"] == "repos/fork/two/contents/new.py?ref=head123"
    assert endpoints[1] == "repos/example/two/compare/base123...head123"
    with pytest.raises(ValueError, match="changed"):
        github.github_file_diff(
            tracked, "main", "new.py", session_id="session", pr_url=B, head_sha="stale"
        )


async def test_changes_route_pins_revision_and_reports_truncation(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    head, base = "a" * 40, "b" * 40

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        if args[-1] == "repos/example/two/pulls/42":
            pr = {"head": {"sha": head}, "base": {"sha": base}, "changed_files": 3001}
            return 0, json.dumps(pr), ""
        assert args[-1] == "repos/example/two/pulls/42/files?per_page=100"
        page = [{"filename": f"f{index}.py", "status": "added"} for index in range(3000)]
        return 0, json.dumps([page]), ""

    monkeypatch.setattr(github, "_gh", gh)
    app = create_runner_app(
        runner_workspace=Path(tracked),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        response = await client.get(
            "/v1/sessions/session/resources/github/changes",
            params={"pr_url": B, "head_sha": head, "base_sha": base},
        )
        partial = await client.get(
            "/v1/sessions/session/resources/github/changes",
            params={"pr_url": B, "head_sha": head},
        )

    assert response.status_code == 200, response.text
    result = response.json()
    assert len(result["data"]) == 3000
    assert {key: result[key] for key in ("has_more", "total_count", "head_sha", "base_sha")} == {
        "has_more": True,
        "total_count": 3001,
        "head_sha": head,
        "base_sha": base,
    }
    assert result["warning"] == "The provider lists at most 3000 files; 1 files are not shown."
    assert partial.status_code == 400
    assert partial.json()["detail"] == "Expected full head and base commit SHAs"


@pytest.mark.parametrize("bad_sha", ["", "abc", "A" * 40, "a" * 39, "a" * 41])
def test_commit_ids_must_be_full_lowercase_shas(
    tracked: str, monkeypatch: pytest.MonkeyPatch, bad_sha: str
) -> None:
    def forbidden_gh(*_args: object, **_kwargs: object) -> None:
        pytest.fail("A malformed commit id must not reach gh")

    monkeypatch.setattr(github, "_gh", forbidden_gh)
    with pytest.raises(ValueError, match="full commit SHA"):
        pr_resource.pr_commits(tracked, session_id="session", pr_url=B, head_sha=bad_sha)
    with pytest.raises(ValueError, match="full commit SHA"):
        pr_resource.pr_commit_diff(tracked, session_id="session", pr_url=B, commit_sha=bad_sha)
    with pytest.raises(ValueError, match="full commit SHA"):
        pr_resource.pr_compare_diff(
            tracked, session_id="session", pr_url=B, head_sha="a" * 40, from_sha=bad_sha
        )


async def test_commits_route_pins_head_and_diff_route_reads_one_commit(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    head, commit = "a" * 40, "c" * 40
    pr = {"head": {"sha": head}, "base": {"sha": "b" * 40}, "commits": 1}
    listing = [
        [
            {
                "sha": commit,
                "commit": {
                    "message": "Fix the thing\n\nDetails",
                    "author": {"name": "Ada", "date": "2026-11-01T10:00:00Z"},
                },
            }
        ]
    ]
    gh_args: list[list[str]] = []

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        gh_args.append(args)
        if args[-1] == "repos/example/two/pulls/42":
            return 0, json.dumps(pr), ""
        if args[-1] == "repos/example/two/pulls/42/commits?per_page=100":
            return 0, json.dumps(listing), ""
        if args[-1] == f"repos/example/two/compare/{commit}...{head}":
            return 0, "compare-patch", ""
        if "graphql" in args:
            return 1, "", "no associated PRs here"
        assert args[-1] == f"repos/example/two/commits/{commit}"
        return 0, "commit-patch", ""

    monkeypatch.setattr(github, "_gh", gh)
    app = create_runner_app(
        runner_workspace=Path(tracked),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        commits = await client.get(
            "/v1/sessions/session/resources/github/commits",
            params={"pr_url": B, "head_sha": head},
        )
        unpinned = await client.get(
            "/v1/sessions/session/resources/github/commits", params={"pr_url": B}
        )
        moved = await client.get(
            "/v1/sessions/session/resources/github/commits",
            params={"pr_url": B, "head_sha": "d" * 40},
        )
        patch = await client.get(
            "/v1/sessions/session/resources/github/commit-diff",
            params={"pr_url": B, "commit_sha": commit},
        )
        malformed = await client.get(
            "/v1/sessions/session/resources/github/commit-diff",
            params={"pr_url": B, "commit_sha": "abc"},
        )
        foreign = await client.get(
            "/v1/sessions/session/resources/github/commit-diff",
            params={"pr_url": B, "commit_sha": "e" * 40},
        )
        compared = await client.get(
            "/v1/sessions/session/resources/github/compare-diff",
            params={"pr_url": B, "head_sha": head, "from_sha": commit},
        )

    assert commits.status_code == 200, commits.text
    assert commits.json() == {
        "object": "list",
        "has_more": False,
        "data": [
            {
                "object": "session.github.commit",
                "sha": commit,
                "title": "Fix the thing",
                "author": "Ada",
                "date": "2026-11-01T10:00:00Z",
            }
        ],
    }
    assert unpinned.status_code == 400
    assert unpinned.json()["detail"] == "Expected head_sha to be a full commit SHA"
    assert moved.status_code == 400
    assert "pull request changed" in moved.json()["detail"]
    assert patch.status_code == 200, patch.text
    assert patch.json() == {"object": "session.github.pr_diff", "patch": "commit-patch"}
    assert compared.status_code == 200, compared.text
    assert compared.json() == {"object": "session.github.pr_diff", "patch": "compare-patch"}
    assert any("Accept: application/vnd.github.diff" in args for args in gh_args)
    assert malformed.status_code == 400
    assert foreign.status_code == 400
    assert foreign.json()["detail"] == "That commit is not part of the selected pull request."


@pytest.fixture
def context_api(monkeypatch: pytest.MonkeyPatch) -> dict[str, tuple[int, str, str]]:
    responses = {
        "pulls": (
            0,
            json.dumps(
                {
                    "head": {"sha": "head123", "repo": {"full_name": "fork/two"}},
                    "base": {"sha": "base123"},
                }
            ),
            "",
        ),
        "compare": (0, json.dumps({"merge_base_commit": {"sha": "merge123"}}), ""),
        "contents": (0, json.dumps({"encoding": "base64", "content": ""}), ""),
    }

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        endpoint = args[-1]
        key = next(key for key in responses if f"/{key}/" in endpoint)
        return responses[key]

    monkeypatch.setattr(github, "_gh", gh)
    return responses


@pytest.mark.parametrize(
    "endpoint,payload",
    [
        ("pulls", "not-json"),
        ("pulls", []),
        ("pulls", {}),
        ("pulls", {"head": None}),
        ("pulls", {"head": {"sha": "head123"}, "base": {}}),
        ("pulls", {"head": {"sha": 123}}),
        ("pulls", {"head": {"sha": ""}}),
        ("pulls", {"head": {"sha": "head123", "repo": {}}, "base": {"sha": "base123"}}),
        ("compare", {}),
        ("compare", {"merge_base_commit": None}),
        ("compare", {"merge_base_commit": {"sha": 123}}),
        ("contents", "not-json"),
        ("contents", []),
        ("contents", {"encoding": "base64"}),
        ("contents", {"encoding": "base64", "content": None}),
        ("contents", {"encoding": "base64", "content": []}),
    ],
)
def test_context_rejects_unexpected_api_responses(
    tracked: str,
    context_api: dict[str, tuple[int, str, str]],
    endpoint: str,
    payload: object,
) -> None:
    context_api[endpoint] = (0, payload if isinstance(payload, str) else json.dumps(payload), "")
    message = (
        "Expanded context is unavailable" if endpoint == "contents" else "unexpected file response"
    )
    with pytest.raises(ValueError, match=message):
        github.github_file_diff(tracked, "main", "new.py", session_id="session", pr_url=B)


@pytest.mark.parametrize("missing", [False, True])
def test_context_preserves_empty_and_missing_files(
    tracked: str, context_api: dict[str, tuple[int, str, str]], missing: bool
) -> None:
    if missing:
        context_api["contents"] = (1, "", "HTTP 404: Not Found")
    result = github.github_file_diff(tracked, "main", "new.py", session_id="session", pr_url=B)
    assert result["before"] == result["after"] == (None if missing else "")


def test_context_reports_deleted_fork(
    tracked: str, context_api: dict[str, tuple[int, str, str]]
) -> None:
    context_api["pulls"] = (
        0,
        json.dumps(
            {
                "head": {"sha": "head123", "repo": None},
                "base": {"sha": "base123"},
            }
        ),
        "",
    )
    with pytest.raises(ValueError, match="head repository is no longer available"):
        github.github_file_diff(tracked, "main", "new.py", session_id="session", pr_url=B)


@pytest.mark.parametrize("pr_url", [None, B])
async def test_pr_reads_report_lock_contention_and_allow_retry(
    tracked: str, monkeypatch: pytest.MonkeyPatch, pr_url: str | None
) -> None:
    monkeypatch.setattr(github, "_gh", lambda *_a, **_kw: (0, '{"number": 42}', ""))
    registry = SessionPrRegistry("session")
    before = registry.path.read_bytes()
    reader = WorkspaceReader(Path(tracked))
    app = create_runner_app(
        runner_workspace=Path(tracked),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        with FileLock(str(registry.path) + ".lock"):
            with pytest.raises(ValueError, match="PR tracking is busy; try again"):
                reader.github_info(session_id="session", pr_url=pr_url)
            response = await client.get(
                "/v1/sessions/session/resources/github",
                params={"pr_url": pr_url} if pr_url else {},
            )
        assert response.status_code == 400
        assert response.json()["detail"] == "PR tracking is busy; try again."
        assert registry.path.read_bytes() == before
        response = await client.get(
            "/v1/sessions/session/resources/github",
            params={"pr_url": pr_url} if pr_url else {},
        )
    assert response.status_code == 200, response.text
    assert {pr["url"] for pr in response.json()["prs"]} == {A, B}
    assert reader.github_info(session_id="session", pr_url=pr_url)["pr"]["number"] == 42


async def test_runner_serves_a_pr_the_server_vouches_for(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh host never recorded the session's PR; the server's vouching is enough."""
    monkeypatch.setattr(github, "_gh", lambda *_a, **_kw: (0, '{"number": 42}', ""))
    app = create_runner_app(
        runner_workspace=Path(tracked),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    path = "/v1/sessions/fresh/resources/github"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        refused = await client.get(path, params={"pr_url": A})
        served = await client.get(path, params={"pr_url": A, "server_tracked": "1"})
        default = await client.get(path, params={"default_pr_url": A, "server_tracked": "1"})
    assert refused.status_code == 400
    assert served.status_code == 200, served.text
    assert served.json()["pr"]["number"] == 42
    assert default.json()["selected_pr_url"] == A
    assert SessionPrRegistry("fresh").list() == []


def _is_panel_view(args: list[str]) -> bool:
    """Whether a ``gh`` call is the selected PR's full panel read."""
    return args[:2] == ["pr", "view"] and "statusCheckRollup" in args[-1]


def test_panel_polls_share_a_recent_read_and_refresh_an_older_one_in_the_background(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    views: list[int] = []
    second_read_may_finish = threading.Event()

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        if _is_panel_view(args):
            views.append(len(views) + 1)
            if len(views) == 2:
                second_read_may_finish.wait(budget(5))
            body = f"Read {len(views)}"
            return 0, json.dumps({"number": 42, "title": "Selected", "body": body}), ""
        return 0, json.dumps({"title": "Listed"}), ""

    monkeypatch.setattr(github, "_gh", gh)
    SessionPrRegistry("session").update_titles({A: "Listed", B: "Selected"})
    poll = functools.partial(
        pr_resource.cached_pr_info, tracked, session_id="session", pr_url=B, allow_cached=True
    )
    first = poll()
    assert first["pr"]["body"] == "Read 1"
    assert "cache_age_s" not in first
    again = poll()
    assert again["pr"]["body"] == "Read 1"
    assert again["cache_age_s"] >= 0
    assert views == [1]

    monkeypatch.setattr(pr_resource, "_PANEL_READ_FRESH_SECONDS", 0.0)
    # Older results answer at once while one background read replaces them.
    assert poll()["pr"]["body"] == "Read 1"
    poll_until(lambda: views == [1, 2], timeout_s=budget(5), interval_s=0.01)
    assert poll()["pr"]["body"] == "Read 1"
    assert views == [1, 2]
    second_read_may_finish.set()
    poll_until(lambda: poll()["pr"]["body"] != "Read 1", timeout_s=budget(5), interval_s=0.01)

    # An expired result is never served: the request waits for the newest read.
    monkeypatch.setattr(pr_resource, "_PANEL_READ_STALE_SECONDS", 0.0)
    assert poll()["pr"]["body"] == f"Read {len(views)}"


def _count_polls_waiting_on_reads(monkeypatch: pytest.MonkeyPatch) -> threading.Semaphore:
    """Release the returned semaphore each time a request waits for, or takes, a shared read."""
    waiting = threading.Semaphore(0)
    served = pr_resource._served

    def counting(read: object, requested_at: float) -> dict[str, object]:
        waiting.release()
        return served(read, requested_at)  # type: ignore[arg-type]

    monkeypatch.setattr(pr_resource, "_served", counting)
    return waiting


def test_concurrent_identical_panel_reads_share_one_forge_read(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    views: list[list[str]] = []
    read_started = threading.Event()
    read_may_finish = threading.Event()

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        if _is_panel_view(args):
            views.append(args)
            read_started.set()
            read_may_finish.wait(budget(5))
        return 0, json.dumps({"number": 42, "title": "Shared"}), ""

    monkeypatch.setattr(github, "_gh", gh)
    waiting = _count_polls_waiting_on_reads(monkeypatch)
    with ThreadPoolExecutor(max_workers=3) as pool:
        reads = [
            pool.submit(
                pr_resource.cached_pr_info,
                tracked,
                session_id="session",
                pr_url=B,
                allow_cached=True,
            )
            for _ in range(3)
        ]
        assert read_started.wait(budget(5))
        # The other two requests are waiting on the running read before it may finish.
        assert all(waiting.acquire(timeout=budget(5)) for _ in range(2))
        read_may_finish.set()
        payloads = [read.result(timeout=budget(5)) for read in reads]
    assert [payload["pr"]["title"] for payload in payloads] == ["Shared"] * 3
    assert len(views) == 1


def test_a_failed_shared_read_reaches_every_waiting_poll_and_the_next_poll_retries(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    reads_started: list[dict[str, object]] = []
    first_read_started = threading.Event()
    first_read_may_fail = threading.Event()
    read_pr_info = pr_resource.pr_info

    def first_read_fails(root: str, **kwargs: Any) -> dict[str, Any]:
        reads_started.append(kwargs)
        if len(reads_started) == 1:
            first_read_started.set()
            first_read_may_fail.wait(budget(5))
            raise ValueError("PR tracking is busy; try again.")
        return read_pr_info(root, **kwargs)

    monkeypatch.setattr(github, "_gh", _forge({"value": "OPEN"}))
    monkeypatch.setattr(pr_resource, "pr_info", first_read_fails)
    waiting = _count_polls_waiting_on_reads(monkeypatch)
    poll = functools.partial(
        pr_resource.cached_pr_info, tracked, session_id="session", pr_url=B, allow_cached=True
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        polls = [pool.submit(poll) for _ in range(2)]
        assert first_read_started.wait(budget(5))
        assert waiting.acquire(timeout=budget(5))
        first_read_may_fail.set()
        for result in polls:
            with pytest.raises(ValueError, match="busy"):
                result.result(timeout=budget(5))
    assert poll()["pr"]["state"] == "OPEN"
    assert len(reads_started) == 2


def test_panel_writes_and_new_tracked_prs_make_the_next_poll_read_again(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    views: list[list[str]] = []

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        if _is_panel_view(args):
            views.append(args)
        return 0, json.dumps({"number": 42, "title": "Tracked"}), ""

    monkeypatch.setattr(github, "_gh", gh)
    SessionPrRegistry("session").update_titles({A: "Tracked", B: "Tracked"})
    poll = functools.partial(
        pr_resource.cached_pr_info, tracked, session_id="session", pr_url=B, allow_cached=True
    )
    poll()
    poll()
    assert len(views) == 1
    # A failed write may still have changed the PR.
    with suppress(ValueError, KeyError):
        pr_resource.create_pr_comment(tracked, session_id="session", pr_url=B, body="Done")
    poll()
    assert len(views) == 2
    SessionPrRegistry("session").record(
        [PullRequestRef.from_url("https://github.com/example/three/pull/7")],
        relationship="worked_on",
        source="test",
    )
    assert len(poll()["prs"]) == 3
    assert len(views) == 3


def test_a_poll_after_a_panel_write_does_not_join_or_keep_a_read_begun_before_it(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    views: list[int] = []
    first_read_started = threading.Event()
    first_read_may_finish = threading.Event()

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        if _is_panel_view(args):
            views.append(number := len(views) + 1)
            if number == 1:
                first_read_started.set()
                first_read_may_finish.wait(budget(5))
            body = f"Read {number}"
            return 0, json.dumps({"number": 42, "title": "Selected", "body": body}), ""
        return 0, json.dumps({"title": "Listed"}), ""

    monkeypatch.setattr(github, "_gh", gh)
    SessionPrRegistry("session").update_titles({A: "Listed", B: "Selected"})
    poll = functools.partial(
        pr_resource.cached_pr_info, tracked, session_id="session", pr_url=B, allow_cached=True
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        earlier = pool.submit(poll)
        assert first_read_started.wait(budget(5))
        # A failed write may still have changed the PR.
        with suppress(ValueError, KeyError):
            pr_resource.create_pr_comment(tracked, session_id="session", pr_url=B, body="Done")
        later = pool.submit(poll)
        assert later.result(timeout=budget(5))["pr"]["body"] == "Read 2"
        first_read_may_finish.set()
        assert earlier.result(timeout=budget(5))["pr"]["body"] == "Read 1"
    assert poll()["pr"]["body"] == "Read 2"


def test_a_failed_registry_check_after_a_read_lets_the_next_poll_read_again(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    views: list[list[str]] = []

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        if _is_panel_view(args):
            views.append(args)
        return 0, json.dumps({"number": 42, "title": "Selected"}), ""

    def unreadable_registry(*_args: object) -> bool:
        raise PermissionError("registry unreadable")

    monkeypatch.setattr(github, "_gh", gh)
    monkeypatch.setattr(pr_resource, "_lists_tracked_prs", unreadable_registry)
    poll = functools.partial(
        pr_resource.cached_pr_info, tracked, session_id="session", pr_url=B, allow_cached=True
    )
    # Uncached titles make the read write the registry, so the check after it runs.
    assert poll()["pr"]["title"] == "Selected"
    assert not [key for key in pr_resource._RUNNING_PANEL_READS if key[0] == tracked]
    poll()
    assert len(views) == 2


async def test_a_reconnected_runner_reads_the_forge_after_an_edit_it_could_not_see(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host serves panel edits while this runner is offline; reconnecting drops older reads."""
    from omnigent.runner._entry import _on_tunnel_reconnect

    views: list[list[str]] = []

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        if _is_panel_view(args):
            views.append(args)
        return 0, json.dumps({"number": 42, "title": "Selected"}), ""

    monkeypatch.setattr(github, "_gh", gh)
    SessionPrRegistry("session").update_titles({A: "Listed", B: "Selected"})
    poll = functools.partial(
        pr_resource.cached_pr_info, tracked, session_id="session", pr_url=B, allow_cached=True
    )
    poll()
    poll()
    assert len(views) == 1
    # The host served an edit that this process never saw.
    caught_up: list[bool] = []

    async def catch_up_scan() -> None:
        caught_up.append(True)

    await _on_tunnel_reconnect(catch_up_scan)()
    poll()
    assert len(views) == 2
    assert caught_up == [True]
    await _on_tunnel_reconnect(None)()


def test_a_read_without_allow_cached_reads_the_forge_even_while_a_poll_runs(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refresh and the other one-shot reads never take an earlier read, finished or running."""
    views: list[int] = []
    first_read_started = threading.Event()
    first_read_may_finish = threading.Event()

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        if _is_panel_view(args):
            views.append(number := len(views) + 1)
            if number == 1:
                first_read_started.set()
                first_read_may_finish.wait(budget(5))
            body = f"Read {number}"
            return 0, json.dumps({"number": 42, "title": "Selected", "body": body}), ""
        return 0, json.dumps({"title": "Listed"}), ""

    monkeypatch.setattr(github, "_gh", gh)
    SessionPrRegistry("session").update_titles({A: "Listed", B: "Selected"})
    read = functools.partial(pr_resource.cached_pr_info, tracked, session_id="session", pr_url=B)
    with ThreadPoolExecutor(max_workers=1) as pool:
        poll = pool.submit(read, allow_cached=True)
        assert first_read_started.wait(budget(5))
        fresh = read()
        first_read_may_finish.set()
        assert poll.result(timeout=budget(5))["pr"]["body"] == "Read 1"
    assert fresh["pr"]["body"] == "Read 2"
    assert "cache_age_s" not in fresh
    assert read()["pr"]["body"] == "Read 3"


def test_a_poll_after_a_pr_is_tracked_does_not_take_an_earlier_read(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll that starts after the agent links a PR lists it, and so do the polls after it."""
    monkeypatch.setattr(github, "_gh", _forge({"value": "OPEN"}))
    listed = threading.Event()
    release = threading.Event()
    list_prs = pr_resource._session_prs_with_titles

    def held_first_listing(*args: Any, **kwargs: Any) -> Any:
        if not listed.is_set():
            listed.set()
            release.wait(budget(5))
        return list_prs(*args, **kwargs)

    monkeypatch.setattr(pr_resource, "_session_prs_with_titles", held_first_listing)
    poll = functools.partial(
        pr_resource.cached_pr_info, tracked, session_id="session", pr_url=B, allow_cached=True
    )
    linked = "https://github.com/example/three/pull/7"
    with ThreadPoolExecutor(max_workers=1) as pool:
        earlier = pool.submit(poll)
        assert listed.wait(budget(5))
        SessionPrRegistry("session").record(
            [PullRequestRef.from_url(linked)], relationship="created", source="test"
        )
        later = poll()
        release.set()
        assert {pr["url"] for pr in earlier.result(timeout=budget(5))["prs"]} == {A, B}
    assert linked in {pr["url"] for pr in later["prs"]}
    assert linked in {pr["url"] for pr in poll()["prs"]}


def _forge(state: dict[str, str]) -> object:
    """A ``gh`` stub whose PR views report ``state["value"]``, as the forge holds it now."""

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        return 0, json.dumps({"number": 42, "title": "Selected", "state": state["value"]}), ""

    return gh


async def test_a_panel_merge_shows_on_the_next_panel_read(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel refetches after its own merge; that read must show the merge."""
    forge = {"value": "OPEN"}
    monkeypatch.setattr(github, "_gh", _forge(forge))
    monkeypatch.setattr(github, "_run_pr_merge", lambda *_a, **_kw: forge.update(value="MERGED"))
    app = create_runner_app(
        runner_workspace=Path(tracked),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    path = "/v1/sessions/session/resources/github"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        before = await client.get(path, params={"pr_url": B})
        merged = await client.post(
            f"{path}/merge",
            json={"pr_url": B, "merge_method": "squash", "expected_head": "a" * 40},
        )
        after = await client.get(path, params={"pr_url": B})
    assert before.json()["pr"]["state"] == "OPEN"
    assert merged.status_code == 200, merged.text
    assert after.json()["pr"]["state"] == "MERGED"


async def test_a_panel_merge_answers_and_shows_when_its_write_cannot_be_recorded(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that can't leave its cross-process record still succeeds, and polls still see it."""
    forge = {"value": "OPEN"}
    monkeypatch.setattr(github, "_gh", _forge(forge))
    monkeypatch.setattr(github, "_run_pr_merge", lambda *_a, **_kw: forge.update(value="MERGED"))
    # A directory where the write record would go makes recording it fail.
    SessionPrRegistry("session").path.with_suffix(".panel-writes").mkdir(parents=True)
    app = create_runner_app(
        runner_workspace=Path(tracked),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    path = "/v1/sessions/session/resources/github"
    poll = {"pr_url": B, "allow_cached": "true"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        before = [await client.get(path, params=poll) for _ in range(2)]
        merged = await client.post(
            f"{path}/merge",
            json={"pr_url": B, "merge_method": "squash", "expected_head": "a" * 40},
        )
        after = await client.get(path, params=poll)
    assert [response.json()["pr"]["state"] for response in before] == ["OPEN", "OPEN"]
    assert merged.status_code == 200, merged.text
    assert after.json()["pr"]["state"] == "MERGED"


_PANEL_COMMENT_IN_ANOTHER_PROCESS = """
import sys
from omnigent.runner import github_resource, pr_resource
github_resource._gh = lambda *_args, **_kwargs: (0, "{}", "")
try:
    pr_resource.create_pr_comment(sys.argv[1], session_id="session", pr_url=sys.argv[2], body="Hi")
except Exception:
    pass  # A failed write may still have changed the PR.
"""


async def test_a_poll_reads_the_forge_after_another_process_writes_through_the_panel(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host serves panel edits while the runner is offline, in its own process."""
    from omnigent.runner._entry import _on_tunnel_reconnect

    views: list[list[str]] = []

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        if _is_panel_view(args):
            views.append(args)
        return 0, json.dumps({"number": 42, "title": "Selected"}), ""

    monkeypatch.setattr(github, "_gh", gh)
    SessionPrRegistry("session").update_titles({A: "Listed", B: "Selected"})
    app = create_runner_app(
        runner_workspace=Path(tracked),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    path = "/v1/sessions/session/resources/github"
    poll = {"pr_url": B, "allow_cached": "true"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        await client.get(path, params=poll)
        other_process = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, "-c", _PANEL_COMMENT_IN_ANOTHER_PROCESS, tracked, B],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=budget(60),
        )
        reads_before = len(views)
        # The runner was offline while the host served the edit, so it reconnects first.
        await _on_tunnel_reconnect(None)()
        after = await client.get(path, params=poll)
    assert other_process.returncode == 0, other_process.stderr
    assert after.status_code == 200, after.text
    assert len(views) == reads_before + 1


async def test_panel_reads_answer_for_their_own_session_and_selected_pr(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sessions sharing a workspace, and PRs within one session, never answer for each other."""

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        repository = args[args.index("-R") + 1] if "-R" in args else "workspace"
        return 0, json.dumps({"number": 42, "title": repository.rsplit("/", 1)[-1]}), ""

    monkeypatch.setattr(github, "_gh", gh)
    other = "https://github.com/example/three/pull/42"
    SessionPrRegistry("other").record(
        [PullRequestRef.from_url(other)], relationship="created", source="test"
    )
    app = create_runner_app(
        runner_workspace=Path(tracked),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        reads = [
            await client.get(f"/v1/sessions/{session}/resources/github", params={"pr_url": url})
            for session, url in [("session", A), ("session", B), ("other", other), ("session", A)]
        ]
    assert [read.status_code for read in reads] == [200] * 4
    assert [read.json()["pr"]["title"] for read in reads] == ["one", "two", "three", "one"]
    assert {pr["url"] for pr in reads[2].json()["prs"]} == {other}
    assert {pr["url"] for pr in reads[3].json()["prs"]} == {A, B}


async def test_a_panel_read_shows_a_forge_change_made_before_it(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refresh, the turn-end refetch and focus refetches are one-shot reads of the forge.

    A PR merged outside the app before such a read must show as merged in it: a
    merged PR stops the panel's poll, so no later poll would correct an older answer.
    """
    forge = {"value": "OPEN"}
    monkeypatch.setattr(github, "_gh", _forge(forge))
    app = create_runner_app(
        runner_workspace=Path(tracked),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    path = "/v1/sessions/session/resources/github"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        before = await client.get(path, params={"pr_url": B})
        forge["value"] = "MERGED"
        refreshed = await client.get(path, params={"pr_url": B})
    assert before.json()["pr"]["state"] == "OPEN"
    assert refreshed.json()["pr"]["state"] == "MERGED"


def test_a_read_after_a_pr_is_tracked_lists_it_while_an_earlier_read_runs(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A read that starts after the agent links a PR lists it, even if an older read is busy."""
    monkeypatch.setattr(github, "_gh", _forge({"value": "OPEN"}))
    listed = threading.Event()
    release = threading.Event()
    list_prs = pr_resource._session_prs_with_titles

    def held_first_listing(*args: Any, **kwargs: Any) -> Any:
        if not listed.is_set():
            listed.set()
            release.wait(budget(5))
        return list_prs(*args, **kwargs)

    monkeypatch.setattr(pr_resource, "_session_prs_with_titles", held_first_listing)
    reader = WorkspaceReader(Path(tracked))
    linked = "https://github.com/example/three/pull/7"
    with ThreadPoolExecutor(max_workers=2) as pool:
        earlier = pool.submit(reader.github_info, session_id="session", pr_url=B)
        assert listed.wait(budget(5))
        SessionPrRegistry("session").record(
            [PullRequestRef.from_url(linked)], relationship="created", source="test"
        )
        later = pool.submit(reader.github_info, session_id="session", pr_url=B)
        release.set()
        assert {pr["url"] for pr in earlier.result(timeout=budget(5))["prs"]} == {A, B}
        assert linked in {pr["url"] for pr in later.result(timeout=budget(5))["prs"]}


async def test_a_poll_after_the_agent_links_a_pr_shows_it_not_an_earlier_setup_state(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A poll may answer with an earlier setup state, but never after the session tracks a PR."""
    monkeypatch.setattr(github, "_git", lambda *_a, **_kw: (128, "", "not a git repository"))
    monkeypatch.setattr(github, "_gh", _forge({"value": "OPEN"}))
    app = create_runner_app(
        runner_workspace=Path(tracked),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    path = "/v1/sessions/untracked/resources/github"
    poll = {"allow_cached": "true"}
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        before = [await client.get(path, params=poll) for _ in range(2)]
        SessionPrRegistry("untracked").record(
            [PullRequestRef.from_url(A)], relationship="created", source="test"
        )
        linked = await client.get(path, params=poll)
    assert [response.json()["reason"] for response in before] == ["not_a_git_repo"] * 2
    assert linked.status_code == 200, linked.text
    assert linked.json()["selected_pr_url"] == A
    assert linked.json()["pr"]["state"] == "OPEN"


def test_host_pr_reads_without_the_poll_flag_reflect_the_forge_now(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host serves an offline runner's panel; its one-shot reads stay fresh after a poll."""
    from omnigent.host.connect import HostProcess

    forge = {"value": "OPEN"}
    monkeypatch.setattr(github, "_gh", _forge(forge))
    reader = WorkspaceReader(Path(tracked))
    polled = HostProcess._dispatch_fs_op(
        reader, "github_info", "session", {"pr_url": B, "allow_cached": True}
    )
    forge["value"] = "MERGED"
    fresh = HostProcess._dispatch_fs_op(reader, "github_info", "session", {"pr_url": B})
    assert polled["pr"]["state"] == "OPEN"  # type: ignore[index]
    assert fresh["pr"]["state"] == "MERGED"  # type: ignore[index]


async def test_pr_panel_reads_answer_while_changed_file_listings_hold_the_slow_pool(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Provider CLI reads have their own threads, so stuck ``git status`` can't delay them."""
    gh_threads: list[str] = []

    def fake_gh(*_args: object, **_kwargs: object) -> tuple[int, str, str]:
        gh_threads.append(threading.current_thread().name)
        return 0, '{"number": 42}', ""

    monkeypatch.setattr(github, "_gh", fake_gh)
    release_git = threading.Event()
    listings_started = [threading.Event() for _ in range(4)]

    def stuck_listing(started: threading.Event) -> None:
        started.set()
        release_git.wait(budget(30))

    listings = [asyncio.create_task(run_slow_io(stuck_listing, e)) for e in listings_started]
    app = create_runner_app(
        runner_workspace=Path(tracked),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    try:
        await poll_until_async(
            lambda: all(e.is_set() for e in listings_started), timeout_s=budget(5)
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://runner"
        ) as client:
            response = await asyncio.wait_for(
                client.get(
                    "/v1/sessions/fresh/resources/github",
                    params={"pr_url": A, "server_tracked": "1"},
                ),
                timeout=budget(5),
            )
    finally:
        release_git.set()
        await asyncio.gather(*listings)

    assert response.status_code == 200, response.text
    assert gh_threads and all(name.startswith("runner-provider-cli_") for name in gh_threads)


@pytest.mark.parametrize("operation", ["github_changes", "github_pr_diff", "github_file_diff"])
def test_selected_pr_reads_report_lock_contention(tracked: str, operation: str) -> None:
    registry = SessionPrRegistry("session")
    reader = WorkspaceReader(Path(tracked))
    kwargs = (
        {"relative_path": "file.py", "base": "main"} if operation == "github_file_diff" else {}
    )
    with FileLock(str(registry.path) + ".lock"):
        with pytest.raises(ValueError, match="PR tracking is busy; try again"):
            getattr(reader, operation)(session_id="session", pr_url=B, **kwargs)


@pytest.mark.parametrize("action", ["attach", "remove"])
async def test_pr_update_reports_lock_contention_and_allows_retry(
    tracked: str, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    monkeypatch.setattr(github, "_gh", lambda *_a, **_kw: (0, '{"number": 42}', ""))
    registry = SessionPrRegistry("session")
    before = registry.path.read_bytes()
    url = A.replace("42", "99") if action == "attach" else A
    app = create_runner_app(
        runner_workspace=Path(tracked),
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        with FileLock(str(registry.path) + ".lock"):
            response = await client.post(
                "/v1/sessions/session/resources/github/prs", json={"url": url, "action": action}
            )
        assert response.status_code == 400
        assert response.json()["detail"] == "PR tracking is busy; try again."
        assert registry.path.read_bytes() == before
        response = await client.post(
            "/v1/sessions/session/resources/github/prs", json={"url": url, "action": action}
        )
    assert response.status_code == 200, response.text
    assert (url in {entry.url for entry in registry.list()}) == (action == "attach")


async def test_shallow_diff_error_preserves_recovery_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The runner returns error.message so the server proxy preserves recovery advice."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    responses = {
        ("rev-parse", "--verify", "--quiet", "origin/main^{commit}"): (0, "main-sha", ""),
        ("merge-base", "origin/main", "HEAD"): (1, "", ""),
        ("rev-parse", "--is-shallow-repository"): (0, "true\n", ""),
    }

    def git(args: list[str], *, cwd: str) -> tuple[int, str, str]:
        assert cwd == str(tmp_path / "session")
        return responses[tuple(args)]

    monkeypatch.setattr(github, "_git", git)
    app = create_runner_app(
        runner_workspace=tmp_path,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        response = await client.get(
            "/v1/sessions/session/resources/github/diff/fileA.py", params={"base": "main"}
        )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["code"] == "invalid_input"
    assert "shallow" in error["message"]
    assert "git fetch --deepen=" in error["message"]
    assert "git fetch --unshallow" in error["message"]


def test_manual_attach_and_exclusion(tracked: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        github, "_gh", lambda *_args, **_kwargs: (0, json.dumps({"number": 99}), "")
    )
    url = B.replace("42", "99")
    info = github.update_session_pr(tracked, "session", url, "attach")
    assert info["selected_pr_url"] == url
    github.update_session_pr(tracked, "session", url, "remove")
    assert url not in {entry.url for entry in SessionPrRegistry("session").list()}


def test_default_selection_matches_metadata_and_all_pages(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        if args[:2] == ["pr", "view"]:
            if args[-1] == github._PR_STATUS_FIELDS:
                assert args[args.index("-R") + 1] == "github.com/example/two"
                return 0, json.dumps({"title": "Second repository"}), ""
            assert args[args.index("-R") + 1] == "github.com/example/one"
            return 0, json.dumps({"number": 42}), ""
        if args[:2] == ["pr", "diff"]:
            assert args[-1] == "github.com/example/one"
            return 0, "first", ""
        assert "--slurp" in args
        assert args[-1].startswith("repos/example/one/pulls/42/")
        return 0, json.dumps([[{"filename": "a.py"}], [{"filename": "b.py"}]]), ""

    monkeypatch.setattr(github, "_gh", gh)
    monkeypatch.setattr(
        github, "_git", lambda *_a, **_kw: pytest.fail("unexpected checkout inference")
    )
    assert github.github_info(tracked, session_id="session")["selected_pr_url"] == A
    assert github.github_pr_diff(tracked, session_id="session")["patch"] == "first"
    assert [
        f["path"] for f in github.github_changed_files(tracked, session_id="session")["data"]
    ] == ["a.py", "b.py"]


def test_enterprise_without_auth_retains_selection(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = A.replace("github.com", "github.example.org")
    SessionPrRegistry("session").record(
        [PullRequestRef.from_url(url)], relationship="created", source="test"
    )
    monkeypatch.setattr(github, "_list_accounts", lambda _: (True, []))

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        if args == ["auth", "status", "-h", "github.example.org"]:
            return 1, "", "You are not logged into any accounts on github.example.org"
        assert args[args.index("-R") + 1] in {
            "github.com/example/one",
            "github.com/example/two",
        }, "unknown host request"
        return 0, '{"title": "Public host PR"}', ""

    monkeypatch.setattr(github, "_gh", gh)
    info = github.github_info(tracked, session_id="session", pr_url=url)
    assert info["selected_pr_url"] == url
    assert info["authenticated"] is False
    assert info["accounts"] == []
    assert info["selected_account"] is None
    assert info["pr"] is None
    assert len(info["prs"]) == 3


@pytest.mark.parametrize(
    "json_supported", [True, False], ids=["listing_omits_host", "gh_without_json"]
)
def test_enterprise_host_signed_in_only_when_named_can_attach_and_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, json_supported: bool
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(github.shutil, "which", lambda _: "/bin/gh")
    monkeypatch.setattr(github._config, "github_account_preference", lambda _: None)
    url = A.replace("github.com", "github.example.org")
    listed = {"hosts": {"github.com": [{"login": "octocat", "active": True, "state": "success"}]}}

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        if args[:4] == ["auth", "status", "--json", "hosts"]:
            if json_supported:
                return 0, json.dumps(listed), ""
            return 1, "", "unknown flag: --json"
        if args == ["auth", "status"]:
            return 0, "Logged in to github.com account octocat", ""
        if args == ["auth", "status", "-h", "github.example.org"]:
            return 0, "Logged in to github.example.org account octocat", ""
        if args[:2] == ["pr", "view"]:
            assert args[args.index("-R") + 1] == "github.example.org/example/one"
            return 0, json.dumps({"number": 42, "url": url, "title": "Enterprise PR"}), ""
        assert args[:3] == ["api", "--hostname", "github.example.org"], args
        if "graphql" in args:
            return 1, "", "not needed"
        return 0, json.dumps([[{"filename": "enterprise.py", "status": "added"}]]), ""

    monkeypatch.setattr(github, "_gh", gh)
    info = github.update_session_pr(str(tmp_path), "session", url, "attach")
    assert info["selected_pr_url"] == url
    assert info["authenticated"] is True
    assert info["pr"]["title"] == "Enterprise PR"
    files = github.github_changed_files(str(tmp_path), session_id="session", pr_url=url)
    assert [entry["path"] for entry in files["data"]] == ["enterprise.py"]
