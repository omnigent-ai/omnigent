"""Explicit PR selection cannot leak back to the session's current checkout."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from filelock import FileLock
from filelock import Timeout as FileLockTimeout

from omnigent.runner import create_runner_app
from omnigent.runner import github_resource as github
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry
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
    info = github.github_info(tracked, session_id="session", pr_url=B)
    assert info["pr"]["title"] == "Second repository"
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
    assert calls[1] == ["pr", "view", "42", "-R", "github.com/example/one", "--json", "title"]
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
        github._PR_VIEW_FIELDS + ",headRefOid,baseRefOid",
        "title",
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


def test_titles_refresh_after_cache_expiry_and_keep_last_known_on_failure(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry = SessionPrRegistry("session")
    registry.update_titles(
        {A: "First", B: "Second"}, timestamp=time.time() - github._PR_TITLE_CACHE_SECONDS - 1
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
    monkeypatch.setattr(github, "time", SimpleNamespace(monotonic=lambda: clock, time=time.time))
    registry = SessionPrRegistry("session")
    registry.record(
        [PullRequestRef.from_url(A.replace("42", str(number))) for number in range(43, 49)],
        relationship="created",
        source="test",
    )
    monkeypatch.setattr(github, "_PR_TITLE_LOOKUP_SECONDS", 0.2)
    attempted: list[str] = []
    title_lookups = Barrier(4, timeout=budget(5))

    def run(argv: list[str], *, timeout: float, **_kwargs: object) -> tuple[int | None, str, str]:
        nonlocal clock
        if argv[-1] != "title":
            assert github._pr_title_deadline.get() is None
            assert timeout == github._gh_timeout_seconds()
            return 0, '{"title": "Selected PR"}', ""
        assert 0 < timeout <= 0.2
        attempted.append(argv[3])
        deadline = github._pr_title_deadline.get()
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
        if argv[-1] != "title":
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
        wall_clock += github._PR_TITLE_TIMEOUT_RETRY_SECONDS + 1
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
    token = github._pr_title_deadline.set(12.0)
    timeout_token = github._pr_title_timed_out.set(False)
    try:
        result = github._pr_json(
            str(tmp_path),
            PullRequestRef.from_url(A.replace("github.com", "github.example.org")),
            "title",
        )
        timed_out = github._pr_title_timed_out.get()
    finally:
        github._pr_title_timed_out.reset(timeout_token)
        github._pr_title_deadline.reset(token)
    if command_seconds == 0.6:
        assert timeouts == pytest.approx([2.0, 1.4, 0.8])
        assert result == {"title": "Enterprise PR"}
        assert timed_out is False
    else:
        assert timeouts == [2.0]
        assert result is None
        assert timed_out is True
    assert github._pr_title_deadline.get() is None


def test_slow_selected_metadata_does_not_get_extra_title_work(
    tracked: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = 10.0
    monkeypatch.setattr(github.time, "monotonic", lambda: clock)

    def gh(args: list[str], **_kwargs: object) -> tuple[int, str, str]:
        nonlocal clock
        assert args[-1] != "title"
        clock += github._PR_TITLE_REQUEST_SECONDS + 0.1
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
        if argv[-1] != "title":
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
    assert github._pr_title_timed_out.get() is False

    wall_clock += github._PR_TITLE_TIMEOUT_RETRY_SECONDS - 0.1
    github.github_info(tracked, session_id="session", pr_url=B)
    assert timeouts == [1.5]

    wall_clock += 0.1
    info = github.github_info(tracked, session_id="session", pr_url=B)
    assert timeouts == [1.5, 1.5]
    assert {entry["url"]: entry["title"] for entry in info["prs"]}[A] == "Recovered title"
    entry = next(entry for entry in registry.list() if entry.url == A)
    assert entry.title_checked_at == wall_clock
    assert entry.title_lookup_timed_out is False
    wall_clock += github._PR_TITLE_TIMEOUT_RETRY_SECONDS
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
        if argv[-1] != "title":
            return 0, '{"title": "Selected PR"}', ""
        attempted += 1
        clock += command_seconds
        return returncode, "", error

    monkeypatch.setattr(github, "_run", run)
    github.github_info(tracked, session_id="session", pr_url=B)
    entry = next(entry for entry in SessionPrRegistry("session").list() if entry.url == A)
    assert entry.title_checked_at == wall_clock
    assert entry.title_lookup_timed_out is False
    for elapsed in [github._PR_TITLE_TIMEOUT_RETRY_SECONDS, github._PR_TITLE_CACHE_SECONDS - 1]:
        wall_clock = 1000.0 + elapsed
        github.github_info(tracked, session_id="session", pr_url=B)
        assert attempted == 1
    wall_clock = 1000.0 + github._PR_TITLE_CACHE_SECONDS
    github.github_info(tracked, session_id="session", pr_url=B)
    assert attempted == 2


def test_unassociated_selection_is_rejected(tracked: str) -> None:
    with pytest.raises(ValueError, match="not associated"):
        github.github_pr_diff(tracked, session_id="session", pr_url=A.replace("42", "99"))


def test_auth_failure_preserves_pr_list(tracked: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(github, "_gh", lambda *_args, **_kwargs: (1, "", "not authenticated"))
    info = github.github_info(tracked, session_id="session", pr_url=B)
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
            if args[-1] == "title":
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
        assert args[args.index("-R") + 1] in {
            "github.com/example/one",
            "github.com/example/two",
        }, "unknown host request"
        return 0, '{"title": "Public host PR"}', ""

    monkeypatch.setattr(github, "_gh", gh)
    info = github.github_info(tracked, session_id="session", pr_url=url)
    assert info["selected_pr_url"] == url
    assert info["pr"] is None
    assert len(info["prs"]) == 3
