"""Which GitHub account each pull request write runs as.

A write runs with the token of the login chosen for the repository. When ``gh`` has no token for
that login on the host, the write is refused before anything is sent, since ``gh`` would otherwise
run it as whichever account is active there.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import pytest

from omnigent.runner import github_resource as github
from omnigent.runner import github_review_threads as review_threads
from omnigent.runner.git_providers import ReviewerRef
from omnigent.runner.session_prs import PullRequestRef

PR = PullRequestRef.from_url("https://github.com/example-org/example-repo/pull/17")
HEAD, BASE = "a" * 40, "b" * 40
ANCHOR = {
    "path": "app.py",
    "side": "RIGHT",
    "line": 12,
    "start_line": None,
    "head_sha": HEAD,
    "base_sha": BASE,
}

GhCall = tuple[list[str], str | None]


def _reads(_root: str, _reference: PullRequestRef, query: str, **_variables: Any) -> dict:
    """The review-conversation reads a reply and a new conversation make before they write."""
    if "PullRequestReviewThread" in query:
        thread = {
            "id": "PRRT_1",
            "viewerCanReply": True,
            "pullRequest": {"url": PR.url},
            "root": {"nodes": [{"fullDatabaseId": "101", "state": "SUBMITTED"}]},
        }
        return {"data": {"node": thread}}
    pull_request = {"id": "PR_17", "headRefOid": HEAD, "baseRefOid": BASE}
    return {"data": {"repository": {"pullRequest": pull_request}}}


WRITES: dict[str, Callable[[], object]] = {
    "add a label": lambda: github._apply_label_edit("/workspace", PR, add=["bug"], remove=[]),
    "remove a label": lambda: github._apply_label_edit("/workspace", PR, add=[], remove=["bug"]),
    "request a reviewer": lambda: github._apply_reviewer_edit(
        "/workspace", PR, add=[ReviewerRef(login="octocat", kind="user")], remove=[]
    ),
    "post a comment": lambda: github._create_comment("/workspace", PR, body="Looks good"),
    "reply to a conversation": lambda: review_threads.reply(
        "/workspace", PR, thread_id="PRRT_1", body="Done"
    ),
    "start a conversation": lambda: review_threads.create_thread(
        "/workspace", PR, body="Why?", anchor=ANCHOR
    ),
    "edit the description": lambda: github._edit_details("/workspace", PR, title="New", body=None),
    "upload an attachment": lambda: github._upload_attachment(
        "/workspace", PR, name="shot.png", content_type="image/png", content=b"\x89PNG"
    ),
}


@pytest.fixture
def sends(monkeypatch: pytest.MonkeyPatch) -> list[GhCall]:
    """Every ``gh`` call other than a token lookup; ``gh`` has no token for any login."""
    calls: list[GhCall] = []

    def gh(
        argv: list[str], *, cwd: str, token: str | None = None, **_options: Any
    ) -> tuple[int, str, str]:
        assert cwd == "/workspace"
        if argv[:2] == ["auth", "token"]:
            return 1, "", f"no oauth token found for github.com account {argv[3]}"
        if argv[-2:] == ["--jq", ".id"]:
            return 0, "12345\n", ""  # The repository read an attachment upload starts with.
        calls.append((argv, token))
        return 1, "", "HTTP 403: Resource not accessible"

    monkeypatch.setattr(github, "_gh", gh)
    monkeypatch.setattr(review_threads, "_graphql", _reads)
    monkeypatch.setattr(github, "_in_sandbox", lambda: False)
    return calls


@pytest.mark.parametrize("write", WRITES.values(), ids=WRITES.keys())
def test_a_write_for_a_signed_out_chosen_login_is_never_sent(
    monkeypatch: pytest.MonkeyPatch, sends: list[GhCall], write: Callable[[], object]
) -> None:
    monkeypatch.setattr(github._config, "github_account_preference", lambda _repo: "octocat")

    with pytest.raises(ValueError, match="account octocat isn't signed in on the host"):
        with github._gh_deadline_scope(time.monotonic() + 30):
            write()

    assert sends == []


@pytest.mark.parametrize("sandbox", [False, True], ids=["no-login-chosen", "sandbox"])
@pytest.mark.parametrize("write", WRITES.values(), ids=WRITES.keys())
def test_a_write_without_a_chosen_login_or_in_a_sandbox_runs_as_the_active_account(
    monkeypatch: pytest.MonkeyPatch,
    sends: list[GhCall],
    write: Callable[[], object],
    sandbox: bool,
) -> None:
    login = "octocat" if sandbox else None
    monkeypatch.setattr(github._config, "github_account_preference", lambda _repo: login)
    monkeypatch.setattr(github, "_in_sandbox", lambda: sandbox)

    with pytest.raises(ValueError) as refused:
        with github._gh_deadline_scope(time.monotonic() + 30):
            write()

    assert "isn't signed in" not in str(refused.value)
    [(argv, token)] = sends
    assert "-X" in argv
    assert token is None


def test_a_login_cleared_during_the_token_lookup_still_refuses(
    monkeypatch: pytest.MonkeyPatch, sends: list[GhCall]
) -> None:
    """Another request clearing the saved login mid-write must not let this one run as another."""
    saved = ["octocat", None]
    monkeypatch.setattr(github._config, "github_account_preference", lambda _repo: saved.pop(0))

    with pytest.raises(ValueError, match="account octocat isn't signed in on the host"):
        with github._gh_deadline_scope(time.monotonic() + 30):
            WRITES["post a comment"]()

    assert sends == []
