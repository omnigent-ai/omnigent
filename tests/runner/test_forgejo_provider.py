"""Forgejo REST data becomes shared-panel payloads for the workspace branch or a tracked PR."""

from __future__ import annotations

import functools
import json
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.git_providers import forgejo as descriptor
from omnigent.git_providers import reset_for_tests
from omnigent.runner import forgejo_client
from omnigent.runner.git_providers.forgejo import PULL_REQUESTS
from omnigent.runner.session_prs import PullRequestRef

HOST = "git.example.test:3443"
URL = f"https://{HOST}/team/app/pulls/7"
API = "/api/v1/repos/team/app"
HEAD, BASE = "a" * 40, "b" * 40
TOKEN = "fj-token"


class FakeForgejo:
    """Serve canned ``/api/v1`` responses by path and record every request."""

    def __init__(self) -> None:
        self.routes: dict[str, httpx.Response | Exception] = {}
        self.requests: list[httpx.Request] = []

    def add(self, path: str, body: Any = None, *, status: int = 200, text: str | None = None):
        self.routes[path] = (
            httpx.Response(status, text=text)
            if text is not None
            else httpx.Response(status, json=body)
        )

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        route = self.routes.get(request.url.path)
        if isinstance(route, Exception):
            raise route
        return route if route is not None else httpx.Response(404, json={"message": "not found"})


@pytest.fixture
def forge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeForgejo:
    """An fj login for ``HOST`` and a fake API behind every client the facet builds."""
    home = tmp_path / "home"
    keys = home / ".local/share/forgejo-cli/keys.json"
    keys.parent.mkdir(parents=True)
    keys.write_text(json.dumps({"hosts": {HOST: {"type": "Application", "token": TOKEN}}}))
    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(descriptor.sys, "platform", "linux")
    for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "OMNIGENT_GIT_PROVIDER_FORGEJO_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    descriptor._login_authorities.cache_clear()
    forgejo_client._cli_logins.cache_clear()
    reset_for_tests()
    fake = FakeForgejo()
    monkeypatch.setattr(
        forgejo_client,
        "ForgejoClient",
        functools.partial(forgejo_client.ForgejoClient, transport=httpx.MockTransport(fake)),
    )
    return fake


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    path = tmp_path / "checkout"
    subprocess.run(["git", "init", "-q", "-b", "feature", str(path)], check=True)
    _git(path, "remote", "add", "origin", f"git@{HOST.split(':')[0]}:dev/app.git")
    _git(path, "remote", "add", "upstream", f"https://{HOST}/team/app.git")
    return path


def _pull(**overrides: Any) -> dict[str, Any]:
    return {
        "number": 7,
        "html_url": f"https://{HOST}/Team/App/pulls/7",
        "title": "Support Forgejo",
        "body": "Adds a **provider**.",
        "state": "open",
        "merged": False,
        "draft": True,
        "user": {"login": "dev", "id": 12},
        "head": {"ref": "feature", "sha": HEAD, "repo": {"full_name": "Dev/App"}},
        "base": {"ref": "main", "sha": "c" * 40},
        "merge_base": BASE,
        "labels": [{"name": "bug", "color": "#E11D21", "description": ""}, {"name": "x"}],
        **overrides,
    }


def _serve_pull(forge: FakeForgejo, **overrides: Any) -> None:
    forge.add(f"{API}/pulls/7", _pull(**overrides))
    forge.add(
        f"{API}/issues/7/comments",
        [
            {
                "id": 5,
                "html_url": f"{URL}#issuecomment-5",
                "user": {"login": "rev", "id": 3},
                "body": "Looks good",
                "created_at": "2026-01-02T00:00:00Z",
            }
        ],
    )
    forge.add(
        f"{API}/pulls/7/reviews",
        [
            {
                "id": 1,
                "html_url": "https://elsewhere.test/x",
                "user": {"login": "rev"},
                "body": "Review summary",
                "submitted_at": "2026-01-01T00:00:00Z",
            },
            {"id": 2, "user": {"login": "rev"}, "body": " ", "submitted_at": "2026-01-03"},
        ],
    )
    forge.add(
        f"{API}/commits/{HEAD}/status",
        {
            "state": "failure",
            "total_count": 5,
            "statuses": [
                {"context": "build", "status": "success", "target_url": "https://ci.test/1"},
                {"context": "lint", "status": "failure", "target_url": ""},
                {"context": "docs", "status": "warning"},
                {"context": "e2e", "status": "pending"},
                {"context": "deploy", "status": "error"},
            ],
        },
    )


def _serve_branch_discovery(forge: FakeForgejo, pulls: list[dict[str, Any]]) -> None:
    forge.add("/api/v1/repos/dev/app", {"full_name": "dev/app", "parent": None})
    forge.add(f"{API}/pulls", pulls)
    forge.add("/api/v1/repos/dev/app/pulls", [])


def test_workspace_branch_pull_request_fills_the_shared_payload(
    forge: FakeForgejo, root: Path
) -> None:
    _serve_branch_discovery(forge, [_pull(number=6, head={"ref": "other"}), _pull()])
    _serve_pull(forge)

    info = PULL_REQUESTS.workspace_info(str(root))

    assert (info["available"], info["provider"], info["branch"]) == (True, "forgejo", "feature")
    assert info["provider_display"]["display_name"] == "Forgejo"
    assert (info["repo"], info["selected_pr_url"], info["base_ref"]) == (
        {"name_with_owner": "team/app"},
        URL,
        "main",
    )
    assert info["auth"] == {
        "authenticated": True,
        "hint": None,
        "cli": None,
        "accounts": None,
        "selected_account": None,
    }
    assert info["warnings"] == []
    pr = info["pr"]
    assert {key: pr[key] for key in ("number", "url", "state", "is_draft", "author")} == {
        "number": 7,
        "url": URL,
        "state": "OPEN",
        "is_draft": True,
        "author": "dev",
    }
    assert (pr["head_ref"], pr["base_ref"], pr["head_sha"], pr["base_sha"]) == (
        "feature",
        "main",
        HEAD,
        BASE,
    )
    assert pr["labels"] == [
        {"name": "bug", "color": "e11d21", "description": None},
        {"name": "x", "color": None, "description": None},
    ]
    assert [(c["body"], c["url"]) for c in pr["comments"]] == [
        ("Review summary", URL),
        ("Looks good", f"{URL}#issuecomment-5"),
    ]
    checks = pr["checks"]
    assert {key: checks[key] for key in ("passing", "failing", "pending", "total")} == {
        "passing": 2,
        "failing": 2,
        "pending": 1,
        "total": 5,
    }
    assert checks["runs"][0] == {"name": "build", "bucket": "passing", "url": "https://ci.test/1"}
    assert checks["runs"][1]["url"] is None


def test_branch_inference_requires_the_source_repository(forge: FakeForgejo, root: Path) -> None:
    _serve_branch_discovery(
        forge, [_pull(head={"ref": "feature", "sha": HEAD, "repo": {"full_name": "other/app"}})]
    )
    _serve_pull(forge)
    info = PULL_REQUESTS.workspace_info(str(root))
    assert (info["pr"], info["auth"]["authenticated"], info["warnings"]) == (None, True, [])


def test_a_fork_parent_is_searched_for_the_branch_pull_request(
    forge: FakeForgejo, root: Path
) -> None:
    _git(root, "remote", "remove", "upstream")
    _serve_branch_discovery(forge, [_pull()])
    forge.add("/api/v1/repos/dev/app", {"parent": {"html_url": f"https://{HOST}/team/app"}})
    _serve_pull(forge)
    assert PULL_REQUESTS.workspace_info(str(root))["selected_pr_url"] == URL


def test_several_branch_pull_requests_ask_for_an_explicit_link(
    forge: FakeForgejo, root: Path
) -> None:
    _serve_branch_discovery(
        forge, [_pull(), _pull(number=8, html_url=f"https://{HOST}/team/app/pulls/8")]
    )
    info = PULL_REQUESTS.workspace_info(str(root))
    assert info["pr"] is None
    assert info["auth"]["authenticated"] is True
    assert "Several pull requests" in info["warnings"][0]


@pytest.mark.parametrize(
    "state,merged,expected",
    [("open", False, "OPEN"), ("closed", False, "CLOSED"), ("closed", True, "MERGED")],
)
def test_tracked_pull_request_state(
    forge: FakeForgejo, tmp_path: Path, state: str, merged: bool, expected: str
) -> None:
    _serve_pull(forge, state=state, merged=merged, draft=False)
    forge.add(f"{API}/commits/{HEAD}/status", {"state": "", "total_count": 0, "statuses": None})
    info = PULL_REQUESTS.reference_info(str(tmp_path), PullRequestRef.from_url(URL))
    assert (info["pr"]["state"], info["pr"]["is_draft"]) == (expected, False)
    assert info["pr"]["checks"]["total"] == 0
    assert forge.requests[0].headers["Authorization"] == f"token {TOKEN}"


@pytest.mark.parametrize(
    "pull",
    [
        _pull(html_url="https://git.example.test:3443/team/app/pulls/8"),
        _pull(html_url="https://evil.test/team/app/pulls/7"),
        _pull(state="merged"),
        _pull(state="open", merged=True),
    ],
)
def test_an_inconsistent_pull_request_is_not_shown(
    forge: FakeForgejo, tmp_path: Path, pull: dict[str, Any]
) -> None:
    forge.add(f"{API}/pulls/7", pull)
    info = PULL_REQUESTS.reference_info(str(tmp_path), PullRequestRef.from_url(URL))
    assert info["pr"] is None and info["warnings"]


def test_codeberg_reads_are_anonymous_and_a_denial_shows_the_sign_in_hint(
    forge: FakeForgejo, tmp_path: Path
) -> None:
    reference = PullRequestRef.from_url("https://codeberg.org/team/app/pulls/7")
    info = PULL_REQUESTS.reference_info(str(tmp_path), reference)
    assert "Authorization" not in forge.requests[0].headers
    assert info["pr"] is None
    assert info["auth"]["authenticated"] is False
    assert "fj -H codeberg.org auth login" in info["auth"]["hint"]


def test_a_timeout_is_not_a_sign_in_problem(forge: FakeForgejo, tmp_path: Path) -> None:
    forge.routes[f"{API}/pulls/7"] = httpx.ReadTimeout("slow")
    reference = PullRequestRef.from_url(URL)
    info = PULL_REQUESTS.reference_info(str(tmp_path), reference)
    assert (info["pr"], info["auth"]["authenticated"]) == (None, True)
    assert PULL_REQUESTS.pr_title(str(tmp_path), reference, time.monotonic() + 5) == (None, True)


def test_title_lookup_and_access_check(forge: FakeForgejo, tmp_path: Path) -> None:
    reference = PullRequestRef.from_url(URL)
    forge.add(f"{API}/pulls/7", _pull(title="  Support Forgejo  "))
    assert PULL_REQUESTS.pr_title(str(tmp_path), reference, time.monotonic() + 5) == (
        "Support Forgejo",
        False,
    )
    PULL_REQUESTS.verify_accessible(str(tmp_path), reference)
    forge.add(f"{API}/pulls/7", status=404)
    with pytest.raises(ValueError):
        PULL_REQUESTS.verify_accessible(str(tmp_path), reference)
    assert PULL_REQUESTS.pr_title(str(tmp_path), reference, time.monotonic() + 5) == (None, False)


def test_outside_a_checkout_reports_not_a_git_repo(forge: FakeForgejo, tmp_path: Path) -> None:
    info = PULL_REQUESTS.workspace_info(str(tmp_path))
    assert (info["available"], info["reason"]) == (False, "not_a_git_repo")
    assert forge.requests == []


def _serve_files(forge: FakeForgejo, *, partial: bool = False) -> None:
    files = [
        {"filename": "src/app.py", "status": "changed", "additions": 2, "deletions": 1},
        {"filename": "src/new.py", "status": "added", "additions": 3, "deletions": 0},
        {"filename": "src/gone.py", "status": "deleted", "additions": 0, "deletions": 4},
        {
            "filename": "src/renamed.py",
            "previous_filename": "src/old.py",
            "status": "renamed",
            "additions": 0,
            "deletions": 0,
        },
    ]
    forge.routes[f"{API}/pulls/7/files"] = httpx.Response(
        200, json=files, headers={"X-HasMore": "false"}
    )
    if partial:
        forge.routes[f"{API}/pulls/7/files"] = httpx.Response(
            200, json=files, headers={"X-HasMore": "true"}
        )


def test_changed_files_map_status_and_line_counts(forge: FakeForgejo, tmp_path: Path) -> None:
    _serve_pull(forge)
    _serve_files(forge)
    listing = PULL_REQUESTS.changed_files(str(tmp_path), PullRequestRef.from_url(URL))
    assert [
        (f["path"], f["status"], f["previous_path"], f["lines_added"], f["lines_removed"])
        for f in listing["data"]
    ] == [
        ("src/app.py", "modified", None, 2, 1),
        ("src/new.py", "created", None, 3, 0),
        ("src/gone.py", "deleted", None, 0, 4),
        ("src/renamed.py", "renamed", "src/old.py", 0, 0),
    ]
    assert (listing["has_more"], listing["warning"]) == (False, None)


def test_a_truncated_file_list_is_marked(
    forge: FakeForgejo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(forgejo_client, "_MAX_PAGES", 1)
    _serve_pull(forge)
    _serve_files(forge, partial=True)
    listing = PULL_REQUESTS.changed_files(str(tmp_path), PullRequestRef.from_url(URL))
    assert listing["has_more"] is True and listing["warning"]


def test_pr_diff_is_the_forge_unified_diff(forge: FakeForgejo, tmp_path: Path) -> None:
    patch = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b\n"
    _serve_pull(forge)
    forge.add(f"{API}/pulls/7.diff", text=patch)
    assert PULL_REQUESTS.pr_diff(str(tmp_path), PullRequestRef.from_url(URL)) == {
        "object": "session.github.pr_diff",
        "patch": patch,
    }


def _expand(tmp_path: Path, path: str, previous: str | None = None, head: str = HEAD):
    return PULL_REQUESTS.file_diff(
        str(tmp_path),
        PullRequestRef.from_url(URL),
        path,
        base="",
        previous_path=previous,
        head_sha=head,
        base_sha=BASE,
    )


def test_file_context_reads_the_merge_base_and_head(forge: FakeForgejo, tmp_path: Path) -> None:
    _serve_pull(forge)
    _serve_files(forge)
    forge.add(f"{API}/raw/src/old.py", text="before\n")
    forge.add(f"{API}/raw/src/renamed.py", text="after\n")
    forge.add(f"{API}/raw/src/new.py", text="new\n")
    renamed = _expand(tmp_path, "src/renamed.py", "src/old.py")
    assert (renamed["before"], renamed["after"]) == ("before\n", "after\n")
    revisions = [r.url.params.get("ref") for r in forge.requests if "/raw/" in r.url.path]
    assert revisions == [BASE, HEAD]
    created = _expand(tmp_path, "src/new.py")
    assert (created["before"], created["after"]) == (None, "new\n")


@pytest.mark.parametrize(
    "path,previous,head",
    [
        ("src/app.py", None, "d" * 40),
        ("src/missing.py", None, HEAD),
        ("src/renamed.py", "src/other.py", HEAD),
        ("../etc/passwd", None, HEAD),
        ("/abs.py", None, HEAD),
    ],
)
def test_file_context_refuses_stale_or_invalid_requests(
    forge: FakeForgejo, tmp_path: Path, path: str, previous: str | None, head: str
) -> None:
    _serve_pull(forge)
    _serve_files(forge)
    with pytest.raises(ValueError):
        _expand(tmp_path, path, previous, head)


def test_binary_file_context_is_unavailable(forge: FakeForgejo, tmp_path: Path) -> None:
    _serve_pull(forge)
    _serve_files(forge)
    forge.routes[f"{API}/raw/src/new.py"] = httpx.Response(200, content=b"\xff\xfe\x00")
    with pytest.raises(ValueError, match="binary"):
        _expand(tmp_path, "src/new.py")


@pytest.mark.parametrize(
    "call",
    [
        lambda root, ref: PULL_REQUESTS.repo_labels(root, ref),
        lambda root, ref: PULL_REQUESTS.edit_labels(root, ref, add=["a"], remove=[]),
        lambda root, ref: PULL_REQUESTS.merge_options(root, ref),
        lambda root, ref: PULL_REQUESTS.merge(root, ref, merge_method="merge", expected_head=HEAD),
        lambda root, ref: PULL_REQUESTS.disable_auto_merge(root, ref),
        lambda root, ref: PULL_REQUESTS.review_candidates(root, ref),
        lambda root, ref: PULL_REQUESTS.edit_reviewers(root, ref, add=[], remove=[]),
        lambda root, ref: PULL_REQUESTS.create_comment(root, ref, body="hi"),
        lambda root, ref: PULL_REQUESTS.edit_details(root, ref, title="t", body=None),
        lambda root, ref: PULL_REQUESTS.upload_attachment(
            root, ref, name="a.png", content_type="image/png", content=b""
        ),
        lambda root, ref: PULL_REQUESTS.set_preference(root, ref, account="x", remote=None),
    ],
)
def test_write_actions_are_unsupported_without_requests(
    forge: FakeForgejo, tmp_path: Path, call: Any
) -> None:
    with pytest.raises(ValueError):
        call(str(tmp_path), PullRequestRef.from_url(URL))
    assert forge.requests == []
    assert PULL_REQUESTS.capabilities.to_json() == {
        "account_switching": False,
        "base_remote_selection": False,
        "line_counts": True,
        "linked_pr_diff": True,
        "label_editing": False,
        "merging": False,
        "reviewer_editing": False,
        "comment_posting": False,
        "details_editing": False,
        "attachment_uploads": False,
    }
