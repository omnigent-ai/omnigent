"""Explicit PR selection cannot leak back to the session's current checkout."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from omnigent.runner import github_resource as github
from omnigent.runner.session_prs import PullRequestRef, SessionPrRegistry

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
    assert calls[1][-1] == "repos/example/two/pulls/42/files?per_page=100"
    assert calls[2][-2:] == ["-R", "github.com/example/two"]


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
    monkeypatch.setattr(github, "_gh", lambda *_a, **_kw: pytest.fail("unknown host request"))
    info = github.github_info(tracked, session_id="session", pr_url=url)
    assert info["selected_pr_url"] == url
    assert info["pr"] is None
    assert len(info["prs"]) == 3
