"""Tests for :mod:`omnigent.runner.git_providers.azure_devops`, the Azure DevOps PR facet.

Nothing here reaches the network. The REST client talks to a recording
:class:`httpx.MockTransport`, token resolution is replaced, and git may use only
local paths: a workspace's ``dev.azure.com`` remote fetches from a local bare
repository through ``url.<bare>.insteadOf``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner import azure_devops_client
from omnigent.runner.azure_devops_client import AzureToken
from omnigent.runner.git_providers import azure_devops as azure_devops_facet
from omnigent.runner.git_providers.azure_devops import PULL_REQUESTS, AzureDevOpsPullRequests
from omnigent.runner.session_prs import PullRequestRef
from tests.budgets import budget
from tests.runner.azure_devops_fixtures import (
    Handler,
    RecordingTransport,
    request_path,
    request_query,
)

pytest_plugins = ["tests.runner.azure_devops_fixtures"]

ORIGIN = "https://dev.azure.com/contoso/web/_git/app"
REPO_API = "/contoso/web/_apis/git/repositories/app"
PULLS = f"{REPO_API}/pullrequests"
PULL = f"{PULLS}/7"
EVALUATIONS = "/contoso/web/_apis/policy/evaluations"
PR_URL = "https://dev.azure.com/contoso/web/_git/app/pullrequest/7"
HEAD = "a" * 40
BASE = "b" * 40
MERGE_BASE = "c" * 40
TOKEN = AzureToken("secret-token", "bearer")
HINT = "Run az login on the host or set AZURE_DEVOPS_EXT_PAT."
INACCESSIBLE = "Cannot access this pull request with the Azure DevOps credentials on the host"
BUILD_POLICY = {"id": "0609b952-1397-4640-95ec-e00a01b2c241", "displayName": "Build"}
REVIEWERS_POLICY = {"id": "fa4e907d-c16b-4a4c-9dfa-4906e5d171dd", "displayName": "Reviewers"}
NO_CAPABILITIES = {
    "account_switching": False,
    "base_remote_selection": False,
    "line_counts": False,
    "linked_pr_diff": False,
}
# Keys of the panel's end-to-end info fixture, less GitHub's legacy fields.
INFO_KEYS = {"object", "available", "branch", "base_ref", "repo", "pr"}
PR_KEYS = {
    "number",
    "title",
    "state",
    "url",
    "is_draft",
    "author",
    "base_ref",
    "head_ref",
    "checks",
    "body",
    "comments",
}
LEGACY_KEYS = {"gh_available", "authenticated", "accounts", "selected_account"}
NO_CHECKS = {"passing": 0, "failing": 0, "pending": 0, "total": 0, "runs": []}
UNAVAILABLE_COMMITS = {
    "object": "session.github.pr_diff",
    "patch": "",
    "unavailable_reason": "commits_unavailable",
    "message": "Commits are unavailable locally. Refresh after the background fetch.",
}


@pytest.fixture(autouse=True)
def offline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, ado_transport: RecordingTransport
) -> None:
    """Give every test a token, no ``az``, git limited to local paths, and the host guard."""
    monkeypatch.setattr(azure_devops_client, "resolve_token", lambda: TOKEN)
    monkeypatch.setattr(azure_devops_client, "_find_az", lambda: None)
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "gitconfig"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path.parent))
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "test@example.com")


@pytest.fixture
def facet(ado_transport: RecordingTransport) -> AzureDevOpsPullRequests:
    return AzureDevOpsPullRequests(transport=ado_transport)


def git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` and return its stripped output."""
    result = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def has_commit(root: Path, commit: str) -> bool:
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{commit}^{{commit}}"], cwd=root, capture_output=True
    )
    return result.returncode == 0


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A checkout of branch ``feature`` whose ``origin`` is the Azure DevOps repository."""
    root = tmp_path / "workspace"
    root.mkdir()
    git(root, "init")
    (root / "README.md").write_text("hello\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "base")
    git(root, "checkout", "-b", "feature")
    git(root, "remote", "add", "origin", ORIGIN)
    return root


@dataclass(frozen=True)
class Forge:
    """A bare repository standing in for Azure DevOps, and two checkouts of it.

    :ivar seed: Has ``main`` and ``feature``, and is on ``feature``.
    :ivar workspace: Has only ``main``; its ``origin`` is :data:`ORIGIN`.
    :ivar bare: The bare repository, which :data:`ORIGIN` reaches through ``insteadOf``.
    """

    seed: Path
    workspace: Path
    base_sha: str
    head_sha: str
    bare: Path


@pytest.fixture
def forge(tmp_path: Path) -> Forge:
    """``feature`` edits, adds, deletes, and renames one file each on top of ``main``."""
    bare = tmp_path / "app.git"
    git(tmp_path, "init", "--bare", str(bare))
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init")
    files = {
        "edit.py": "one\ntwo\nthree\n",
        "gone.py": "bye\n",
        "old_name.py": "moved\ncontent\nhere\n",
        "keep.py": "keep\n",
    }
    for name, text in files.items():
        (seed / name).write_text(text)
    git(seed, "add", ".")
    git(seed, "commit", "-m", "base")
    git(seed, "branch", "-M", "main")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "origin", "main")
    git(seed, "checkout", "-b", "feature")
    (seed / "edit.py").write_text("one\n2\nthree\n")
    (seed / "added.py").write_text("new\n")
    git(seed, "rm", "gone.py")
    git(seed, "mv", "old_name.py", "new_name.py")
    git(seed, "add", ".")
    git(seed, "commit", "-m", "feature")
    git(seed, "push", "origin", "feature")

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    git(workspace, "init")
    git(workspace, "remote", "add", "origin", ORIGIN)
    git(workspace, "config", f"url.{bare}.insteadOf", ORIGIN)
    git(workspace, "fetch", "origin", "main")
    git(workspace, "checkout", "-b", "main", "FETCH_HEAD")
    return Forge(
        seed, workspace, git(seed, "rev-parse", "main"), git(seed, "rev-parse", "feature"), bare
    )


@pytest.fixture
def fetches(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, float]]:
    """Record the ref and the timeout of every ``git fetch`` the facet runs."""
    recorded: list[tuple[str, float]] = []
    real_git = azure_devops_facet._git

    def recording_git(
        root: str, *args: str, **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes] | None:
        if args[0] == "fetch":
            recorded.append((args[-1], kwargs["timeout"]))
        return real_git(root, *args, **kwargs)

    monkeypatch.setattr(azure_devops_facet, "_git", recording_git)
    return recorded


@dataclass(frozen=True)
class HeldFetch:
    """Holds every ``git fetch`` the facet runs until :attr:`release` is set.

    :ivar finished: Set when a released fetch has run.
    :ivar refs: The ref of each fetch, in the order they started.
    """

    release: threading.Event
    finished: threading.Event
    refs: list[str]


@pytest.fixture
def held_fetch(monkeypatch: pytest.MonkeyPatch) -> Iterator[HeldFetch]:
    """Make every fetch wait for the test, like a fetch of a large repository."""
    held = HeldFetch(threading.Event(), threading.Event(), [])
    real_git = azure_devops_facet._git

    def holding_git(
        root: str, *args: str, **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes] | None:
        if args[0] != "fetch":
            return real_git(root, *args, **kwargs)
        held.refs.append(args[-1])
        held.release.wait(budget(10))
        try:
            return real_git(root, *args, **kwargs)
        finally:
            held.finished.set()

    monkeypatch.setattr(azure_devops_facet, "_git", holding_git)
    yield held
    held.release.set()


def pull_request(
    number: int = 7, status: str = "active", *, head: str = HEAD, base: str = BASE, **fields: Any
) -> dict[str, Any]:
    """Return a pull request object as the REST API sends it."""
    return {
        "pullRequestId": number,
        "status": status,
        "title": "Add the pipeline",
        "description": "## Summary\n\nAdds CI.",
        "isDraft": False,
        "createdBy": {"displayName": "Pat Example", "id": "user-1"},
        "sourceRefName": "refs/heads/feature",
        "targetRefName": "refs/heads/main",
        "lastMergeSourceCommit": {"commitId": head},
        "lastMergeTargetCommit": {"commitId": base},
        "repository": {"name": "app", "project": {"id": "project-1", "name": "web"}},
        **fields,
    }


def reference(number: int = 7, repository: str = "contoso/web/app") -> PullRequestRef:
    org, project, repo = repository.split("/")
    return PullRequestRef(
        provider="azure_devops",
        host="dev.azure.com",
        repository=repository,
        number=number,
        url=f"https://dev.azure.com/{org}/{project}/_git/{repo}/pullrequest/{number}",
    )


def serve(
    transport: RecordingTransport,
    pr: dict[str, Any],
    *,
    statuses: list[dict[str, Any]] | None = None,
    evaluations: list[dict[str, Any]] | None = None,
    threads: list[dict[str, Any]] | None = None,
) -> None:
    """Route one pull request and the lists its payload reads."""
    path = f"{PULLS}/{pr['pullRequestId']}"
    transport.route("GET", path, json=pr)
    transport.route("GET", f"{path}/statuses", json={"value": statuses or []})
    transport.route("GET", EVALUATIONS, json={"value": evaluations or []})
    transport.route("GET", f"{path}/threads", json={"value": threads or []})


def fail(
    transport: RecordingTransport, path: str, failure: int | type[httpx.TransportError]
) -> None:
    """Make requests for ``path`` fail with an HTTP status or a transport error."""
    if isinstance(failure, int):
        transport.route("GET", path, status=failure, json={"message": "TF400813"})
        return

    def handler(request: httpx.Request) -> httpx.Response:
        raise failure("request failed", request=request)

    transport.route("GET", path, handler=handler)


def when_all_started(barrier: threading.Barrier, body: Any) -> Handler:
    """Answer with ``body`` once ``barrier.parties`` requests are waiting at the same time."""

    def handler(_request: httpx.Request) -> httpx.Response:
        barrier.wait()
        return httpx.Response(200, json=body)

    return handler


def never_answers(request: httpx.Request) -> httpx.Response:
    """Act as a server that never answers: wait out the request's timeout, then time out."""
    time.sleep(request.extensions["timeout"]["read"])
    raise httpx.ReadTimeout("timed out", request=request)


# ---------------------------------------------------------------------------
# Info payloads
# ---------------------------------------------------------------------------


def test_workspace_info_shows_the_branch_pr_without_legacy_fields(
    facet: AzureDevOpsPullRequests, workspace: Path, ado_transport: RecordingTransport
) -> None:
    # The active PR wins over a newer completed one.
    ado_transport.route(
        "GET", PULLS, json={"value": [pull_request(9, "completed"), pull_request(7)]}
    )
    serve(ado_transport, pull_request(7))

    info = facet.workspace_info(str(workspace))

    assert info.keys() >= INFO_KEYS | {"provider", "auth", "capabilities"}
    assert not info.keys() & LEGACY_KEYS
    assert info["pr"].keys() >= PR_KEYS
    assert {key: value for key, value in info.items() if key != "pr"} == {
        "object": "session.github.info",
        "available": True,
        "provider": "azure_devops",
        "auth": {
            "authenticated": True,
            "hint": HINT,
            "cli": {"name": "az", "available": False},
            "accounts": None,
            "selected_account": None,
        },
        "capabilities": NO_CAPABILITIES,
        "branch": "feature",
        "base_ref": "main",
        "repo": {"name_with_owner": "contoso/web/app"},
    }
    assert info["pr"] == {
        "number": 7,
        "url": PR_URL,
        "title": "Add the pipeline",
        "state": "OPEN",
        "is_draft": False,
        "author": "Pat Example",
        "author_id": "user-1",
        "base_ref": "main",
        "head_ref": "feature",
        "head_sha": HEAD,
        "base_sha": BASE,
        "checks": NO_CHECKS,
        "body": "## Summary\n\nAdds CI.",
        "comments": [],
    }
    find, *_ = ado_transport.requests
    assert ("searchCriteria.sourceRefName", "refs/heads/feature") in request_query(find)
    [evaluations] = [r for r in ado_transport.requests if request_path(r) == EVALUATIONS]
    artifact = ("artifactId", "vstfs:///CodeReview/CodeReviewId/project-1/7")
    assert artifact in request_query(evaluations)


def test_without_an_active_pr_the_newest_is_shown(
    facet: AzureDevOpsPullRequests, workspace: Path, ado_transport: RecordingTransport
) -> None:
    ado_transport.route(
        "GET", PULLS, json={"value": [pull_request(5, "completed"), pull_request(3, "abandoned")]}
    )
    serve(ado_transport, pull_request(5, "completed"))

    pr = facet.workspace_info(str(workspace))["pr"]

    assert (pr["number"], pr["state"]) == (5, "MERGED")


def test_workspace_info_without_a_pr(
    facet: AzureDevOpsPullRequests, workspace: Path, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", PULLS, json={"value": []})

    info = facet.workspace_info(str(workspace))

    assert info["repo"] == {"name_with_owner": "contoso/web/app"}
    assert info["pr"] is None
    assert info["base_ref"] is None
    assert info["auth"]["authenticated"] is True
    assert len(ado_transport.requests) == 1


def test_workspace_info_without_a_token_sends_no_request(
    facet: AzureDevOpsPullRequests,
    workspace: Path,
    ado_transport: RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(azure_devops_client, "resolve_token", lambda: None)
    monkeypatch.setattr(azure_devops_client, "_find_az", lambda: "/opt/homebrew/bin/az")

    info = facet.workspace_info(str(workspace))

    assert info["auth"] == {
        "authenticated": False,
        "hint": HINT,
        "cli": {"name": "az", "available": True},
        "accounts": None,
        "selected_account": None,
    }
    assert info["repo"] == {"name_with_owner": "contoso/web/app"}
    assert info["pr"] is None
    assert ado_transport.requests == []


@pytest.mark.parametrize(("status", "authenticated"), [(401, False), (403, False), (500, True)])
def test_only_a_rejected_credential_reads_as_signed_out(
    facet: AzureDevOpsPullRequests,
    workspace: Path,
    ado_transport: RecordingTransport,
    status: int,
    authenticated: bool,
) -> None:
    ado_transport.route("GET", PULLS, status=status, json={"message": "TF400813"})

    info = facet.workspace_info(str(workspace))

    assert info["auth"]["authenticated"] is authenticated
    assert info["pr"] is None
    assert info["warnings"]


@pytest.mark.parametrize("failure", [503, httpx.ReadTimeout], ids=["503", "timeout"])
def test_a_branch_pr_that_fails_to_load_is_shown_from_its_list_entry(
    facet: AzureDevOpsPullRequests,
    workspace: Path,
    ado_transport: RecordingTransport,
    failure: int | type[httpx.TransportError],
) -> None:
    # The PR list truncates descriptions.
    ado_transport.route("GET", PULLS, json={"value": [pull_request(description="## Summ")]})
    serve(ado_transport, pull_request(), statuses=[status("build", "succeeded", 1)])
    fail(ado_transport, PULL, failure)

    info = facet.workspace_info(str(workspace))

    pr = info["pr"]
    assert (pr["number"], pr["url"], pr["title"], pr["body"]) == (
        7,
        PR_URL,
        "Add the pipeline",
        "## Summ",
    )
    assert (pr["head_ref"], pr["base_ref"], info["base_ref"]) == ("feature", "main", "main")
    assert (pr["checks"]["passing"], pr["checks"]["total"]) == (1, 1)
    assert info["auth"]["authenticated"] is True
    assert info["warnings"]


def test_a_denied_pr_read_after_the_list_reads_as_signed_out(
    facet: AzureDevOpsPullRequests, workspace: Path, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", PULLS, json={"value": [pull_request()]})
    serve(ado_transport, pull_request())
    fail(ado_transport, PULL, 401)

    info = facet.workspace_info(str(workspace))

    assert info["pr"] is None
    assert info["auth"]["authenticated"] is False


def test_outside_a_git_checkout(
    facet: AzureDevOpsPullRequests, tmp_path: Path, ado_transport: RecordingTransport
) -> None:
    assert facet.workspace_info(str(tmp_path)) == {
        "object": "session.github.info",
        "available": False,
        "reason": "not_a_git_repo",
        "provider": "azure_devops",
        "auth": None,
        "capabilities": NO_CAPABILITIES,
    }
    assert ado_transport.requests == []


def test_origin_names_the_repository_with_its_case(
    facet: AzureDevOpsPullRequests, workspace: Path, ado_transport: RecordingTransport
) -> None:
    git(workspace, "remote", "remove", "origin")
    git(workspace, "remote", "add", "fork", "https://dev.azure.com/contoso/other/_git/lib")
    git(workspace, "remote", "add", "origin", "https://dev.azure.com/Contoso/My%20Web/_git/App")
    pulls = "/Contoso/My%20Web/_apis/git/repositories/App/pullrequests"
    ado_transport.route("GET", pulls, json={"value": [pull_request()]})
    ado_transport.route("GET", f"{pulls}/7", json=pull_request())

    info = facet.workspace_info(str(workspace))

    assert info["repo"] == {"name_with_owner": "Contoso/My Web/App"}
    # Tracked PRs dedupe on the lower-cased canonical URL.
    assert info["pr"]["url"] == "https://dev.azure.com/contoso/my%20web/_git/app/pullrequest/7"


@pytest.mark.parametrize(
    ("status", "state"), [("active", "OPEN"), ("completed", "MERGED"), ("abandoned", "CLOSED")]
)
def test_reference_info_maps_the_pr_status(
    facet: AzureDevOpsPullRequests,
    tmp_path: Path,
    ado_transport: RecordingTransport,
    status: str,
    state: str,
) -> None:
    serve(ado_transport, pull_request(status=status))

    info = facet.reference_info(str(tmp_path), reference())

    assert info["pr"]["state"] == state
    assert info["pr"]["url"] == PR_URL
    assert info["selected_pr_url"] == PR_URL
    assert (info["branch"], info["base_ref"]) == ("feature", "main")
    assert info["repo"] == {"name_with_owner": "contoso/web/app"}
    assert info["auth"]["authenticated"] is True


def test_reference_info_when_the_pr_cannot_be_read(
    facet: AzureDevOpsPullRequests, tmp_path: Path, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", PULL, status=404, json={"message": "TF401180"})

    info = facet.reference_info(str(tmp_path), reference())

    assert info["pr"] is None
    assert info["auth"]["authenticated"] is False
    assert info["auth"]["hint"] == HINT
    assert info["selected_pr_url"] == PR_URL
    assert not LEGACY_KEYS & info.keys()


@pytest.mark.parametrize(
    ("failure", "authenticated"),
    [
        (401, False),
        (403, False),
        (500, True),
        (httpx.ReadTimeout, True),
        (httpx.ConnectError, True),
    ],
    ids=["401", "403", "500", "timeout", "network"],
)
def test_reference_info_reads_as_signed_out_only_when_denied(
    facet: AzureDevOpsPullRequests,
    tmp_path: Path,
    ado_transport: RecordingTransport,
    failure: int | type[httpx.TransportError],
    authenticated: bool,
) -> None:
    fail(ado_transport, PULL, failure)

    info = facet.reference_info(str(tmp_path), reference())

    assert info["pr"] is None
    assert info["auth"]["authenticated"] is authenticated
    assert info["selected_pr_url"] == PR_URL
    assert info["warnings"]


# ---------------------------------------------------------------------------
# Checks and comments
# ---------------------------------------------------------------------------


def status(name: str, state: str, status_id: int, url: str | None = None) -> dict[str, Any]:
    return {
        "id": status_id,
        "state": state,
        "context": {"genre": "ci", "name": name},
        "targetUrl": url,
    }


def evaluation(
    status: str,
    name: str,
    *,
    build_id: int | None = None,
    policy_type: dict[str, str] = BUILD_POLICY,
) -> dict[str, Any]:
    return {
        "status": status,
        "configuration": {"type": policy_type, "settings": {"displayName": name}},
        "context": {"buildId": build_id},
    }


def test_checks_merge_the_latest_statuses_with_build_policy_evaluations(
    facet: AzureDevOpsPullRequests, tmp_path: Path, ado_transport: RecordingTransport
) -> None:
    statuses = [
        status("build", "failed", 1),
        status("lint", "error", 2),
        status("deploy", "pending", 3),
        status("scan", "notSet", 4),
        status("build", "succeeded", 5, "https://ci.example/5"),
        status("docs", "notApplicable", 6),
    ]
    evaluations = [
        evaluation("approved", "Unit tests", build_id=11),
        evaluation("rejected", "Integration", build_id=12),
        evaluation("broken", "Packaging"),
        evaluation("queued", "Nightly"),
        evaluation("running", "E2E"),
        evaluation("notApplicable", "Skipped"),
        evaluation("rejected", "Two reviewers", policy_type=REVIEWERS_POLICY),
    ]
    serve(ado_transport, pull_request(), statuses=statuses, evaluations=evaluations)

    checks = facet.reference_info(str(tmp_path), reference())["pr"]["checks"]

    build = "https://dev.azure.com/contoso/web/_build/results?buildId="
    assert checks == {
        "passing": 2,
        "failing": 3,
        "pending": 4,
        "total": 9,
        "runs": [
            {"name": "ci/build", "bucket": "passing", "url": "https://ci.example/5"},
            {"name": "ci/lint", "bucket": "failing", "url": None},
            {"name": "ci/deploy", "bucket": "pending", "url": None},
            {"name": "ci/scan", "bucket": "pending", "url": None},
            {"name": "Unit tests", "bucket": "passing", "url": f"{build}11"},
            {"name": "Integration", "bucket": "failing", "url": f"{build}12"},
            {"name": "Packaging", "bucket": "failing", "url": None},
            {"name": "Nightly", "bucket": "pending", "url": None},
            {"name": "E2E", "bucket": "pending", "url": None},
        ],
    }


def test_unreadable_checks_and_comments_are_marked_incomplete(
    facet: AzureDevOpsPullRequests, tmp_path: Path, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", PULL, json=pull_request())
    for path in (f"{PULL}/statuses", EVALUATIONS, f"{PULL}/threads"):
        ado_transport.route("GET", path, status=500, json={"message": "unavailable"})

    info = facet.reference_info(str(tmp_path), reference())

    assert info["pr"]["checks"] == {**NO_CHECKS, "partial": True}
    assert info["pr"]["comments_partial"] is True
    assert info["pr"]["comments"] == []
    assert info["auth"]["authenticated"] is True


@pytest.mark.parametrize("failed", ["statuses", "policies", "comments"])
def test_failed_optional_reads_preserve_the_other_loaded_data(
    facet: AzureDevOpsPullRequests, tmp_path: Path, ado_transport: RecordingTransport, failed: str
) -> None:
    serve(
        ado_transport,
        pull_request(),
        statuses=[status("build", "succeeded", 1)],
        evaluations=[evaluation("approved", "Policy build")],
        threads=[{"id": 1, "comments": [comment("Keep this comment")]}],
    )
    path = {
        "statuses": f"{PULL}/statuses",
        "policies": EVALUATIONS,
        "comments": f"{PULL}/threads",
    }[failed]
    fail(ado_transport, path, 503)

    pr = facet.reference_info(str(tmp_path), reference())["pr"]

    assert pr["checks"]["passing"] == (2 if failed == "comments" else 1)
    assert bool(pr["checks"].get("partial")) is (failed != "comments")
    assert bool(pr.get("comments_partial")) is (failed == "comments")
    assert [row["body"] for row in pr["comments"]] == (
        [] if failed == "comments" else ["Keep this comment"]
    )


def test_capped_comments_are_marked_incomplete(
    facet: AzureDevOpsPullRequests, tmp_path: Path, ado_transport: RecordingTransport
) -> None:
    serve(
        ado_transport,
        pull_request(),
        threads=[{"id": 1, "comments": [comment(str(number)) for number in range(101)]}],
    )

    pr = facet.reference_info(str(tmp_path), reference())["pr"]

    assert [row["body"] for row in pr["comments"]] == [str(number) for number in range(100)]
    assert pr["comments_partial"] is True


def comment(
    content: str, *, author: str = "Ada", comment_type: str = "text", deleted: bool = False
) -> dict[str, Any]:
    return {
        "content": content,
        "author": {"displayName": author, "id": f"{author.lower()}-id"},
        "publishedDate": "2026-09-01T10:00:00Z",
        "commentType": comment_type,
        "isDeleted": deleted,
    }


def test_comments_skip_deleted_and_system_entries_and_prefix_inline_ones(
    facet: AzureDevOpsPullRequests, tmp_path: Path, ado_transport: RecordingTransport
) -> None:
    threads = [
        {"id": 1, "isDeleted": True, "comments": [comment("In a deleted thread")]},
        {
            "id": 2,
            "comments": [
                comment("Ada voted 10", comment_type="system"),
                comment("Looks good"),
                comment("Never mind", deleted=True),
            ],
        },
        {
            "id": 3,
            "threadContext": {"filePath": "/src/app.py", "rightFileStart": {"line": 12}},
            "comments": [comment("Rename this", author="Bo"), comment("Done")],
        },
        {
            "id": 4,
            "threadContext": {"filePath": "/src/old.py", "leftFileStart": {"line": 3}},
            "comments": [comment("Why remove it?")],
        },
        {"id": 5, "threadContext": {"filePath": "/README.md"}, "comments": [comment("Typo")]},
    ]
    serve(ado_transport, pull_request(), threads=threads)

    comments = facet.reference_info(str(tmp_path), reference())["pr"]["comments"]

    def shaped(body: str, thread: int, author: str = "Ada") -> dict[str, Any]:
        return {
            "author": author,
            "author_id": f"{author.lower()}-id",
            "body": body,
            "created_at": "2026-09-01T10:00:00Z",
            "url": f"{PR_URL}?discussionId={thread}",
        }

    assert comments == [
        shaped("Looks good", 2),
        shaped("`src/app.py:12`\n\nRename this", 3, "Bo"),
        shaped("`src/app.py:12`\n\nDone", 3),
        shaped("`src/old.py:3`\n\nWhy remove it?", 4),
        shaped("`README.md`\n\nTypo", 5),
    ]


# ---------------------------------------------------------------------------
# Concurrent reads and the request budget
# ---------------------------------------------------------------------------


def test_workspace_info_reads_the_pr_and_its_lists_at_once_on_one_client(
    facet: AzureDevOpsPullRequests,
    workspace: Path,
    ado_transport: RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ado_transport.route("GET", PULLS, json={"value": [pull_request()]})
    # Each read waits until all four have started, so reads sent one at a time fail.
    started = threading.Barrier(4, timeout=budget(10))
    ado_transport.route("GET", PULL, handler=when_all_started(started, pull_request()))
    for path in (f"{PULL}/statuses", EVALUATIONS, f"{PULL}/threads"):
        ado_transport.route("GET", path, handler=when_all_started(started, {"value": []}))
    deadlines: list[float | None] = []
    real_client = azure_devops_client.AzureDevOpsClient

    def counting_client(*args: Any, **kwargs: Any) -> azure_devops_client.AzureDevOpsClient:
        deadlines.append(kwargs.get("deadline"))
        return real_client(*args, **kwargs)

    monkeypatch.setattr(azure_devops_client, "AzureDevOpsClient", counting_client)

    info = facet.workspace_info(str(workspace))

    assert (info["pr"]["number"], info["pr"]["body"]) == (7, "## Summary\n\nAdds CI.")
    [deadline] = deadlines
    assert deadline is not None
    assert len(ado_transport.requests) == 5


def test_reference_info_reads_the_lists_at_once_after_the_pr(
    facet: AzureDevOpsPullRequests, tmp_path: Path, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", PULL, json=pull_request())
    started = threading.Barrier(3, timeout=budget(10))
    for path in (f"{PULL}/statuses", EVALUATIONS, f"{PULL}/threads"):
        ado_transport.route("GET", path, handler=when_all_started(started, {"value": []}))

    info = facet.reference_info(str(tmp_path), reference())

    assert info["pr"]["number"] == 7
    # The evaluations need the project id that the PR carries.
    assert request_path(ado_transport.requests[0]) == PULL
    assert len(ado_transport.requests) == 4


def test_reads_that_outlast_the_budget_mark_checks_and_comments_incomplete(
    facet: AzureDevOpsPullRequests,
    workspace: Path,
    ado_transport: RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seconds = budget(0.5)
    monkeypatch.setattr(azure_devops_facet, "_REQUEST_BUDGET_SECONDS", seconds)
    ado_transport.route("GET", PULLS, json={"value": [pull_request()]})
    ado_transport.route("GET", PULL, json=pull_request())
    slow = (f"{PULL}/statuses", EVALUATIONS, f"{PULL}/threads")
    for path in slow:
        ado_transport.route("GET", path, handler=never_answers)

    info = facet.workspace_info(str(workspace))

    assert (info["pr"]["title"], info["pr"]["body"]) == (
        "Add the pipeline",
        "## Summary\n\nAdds CI.",
    )
    assert (info["pr"]["checks"], info["pr"]["comments"]) == ({**NO_CHECKS, "partial": True}, [])
    assert info["pr"]["comments_partial"] is True
    assert info["auth"]["authenticated"] is True
    timeouts = [
        request.extensions["timeout"]["read"]
        for request in ado_transport.requests
        if request_path(request) in slow
    ]
    assert len(timeouts) == 3
    assert all(0 < timeout <= seconds for timeout in timeouts)


def test_a_list_that_spends_the_budget_leaves_the_pr_from_its_entry(
    facet: AzureDevOpsPullRequests,
    workspace: Path,
    ado_transport: RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(azure_devops_facet, "_REQUEST_BUDGET_SECONDS", budget(0.5))

    def late_list(request: httpx.Request) -> httpx.Response:
        # The answer arrives just after the time left runs out.
        time.sleep(request.extensions["timeout"]["read"] + 0.05)
        return httpx.Response(200, json={"value": [pull_request(description="## Summ")]})

    ado_transport.route("GET", PULLS, handler=late_list)
    serve(ado_transport, pull_request())

    info = facet.workspace_info(str(workspace))

    assert [request_path(request) for request in ado_transport.requests] == [PULLS]
    assert (info["pr"]["number"], info["pr"]["body"]) == (7, "## Summ")
    assert (info["pr"]["checks"], info["pr"]["comments"]) == ({**NO_CHECKS, "partial": True}, [])
    assert info["pr"]["comments_partial"] is True
    assert (info["base_ref"], info["auth"]["authenticated"]) == ("main", True)


# ---------------------------------------------------------------------------
# Changed files and diffs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("change_type", "expected"),
    [
        ("add", "created"),
        ("edit", "modified"),
        ("delete", "deleted"),
        ("rename", "renamed"),
        ("edit, rename", "renamed"),
    ],
)
def test_changed_files_map_change_types(
    facet: AzureDevOpsPullRequests,
    tmp_path: Path,
    ado_transport: RecordingTransport,
    change_type: str,
    expected: str,
) -> None:
    ado_transport.route(
        "GET", f"{PULL}/iterations", json={"value": [{"id": 1}, {"id": 3}, {"id": 2}]}
    )
    changes = [
        {"changeType": "add", "item": {"path": "/src", "isFolder": True, "gitObjectType": "tree"}},
        {"changeType": change_type, "item": {"path": "/src/app.py", "gitObjectType": "blob"}},
    ]
    ado_transport.route("GET", f"{PULL}/iterations/3/changes", json={"changeEntries": changes})

    result = facet.changed_files(str(tmp_path), reference())

    assert result == {
        "object": "list",
        "has_more": False,
        "data": [
            {
                "object": "session.github.changed_file",
                "path": "src/app.py",
                "name": "app.py",
                "status": expected,
                "lines_added": None,
                "lines_removed": None,
            }
        ],
    }
    assert ("$compareTo", "0") in request_query(ado_transport.requests[-1])


def test_changed_files_without_a_branch_pr_are_empty(
    facet: AzureDevOpsPullRequests, workspace: Path, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", PULLS, json={"value": []})

    assert facet.changed_files(str(workspace), None) == {
        "object": "list",
        "data": [],
        "has_more": False,
    }


def test_changed_files_cut_short_by_the_budget_keep_the_pages_read(
    facet: AzureDevOpsPullRequests,
    tmp_path: Path,
    ado_transport: RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seconds = budget(0.5)
    monkeypatch.setattr(azure_devops_facet, "_REQUEST_BUDGET_SECONDS", seconds)
    ado_transport.route("GET", f"{PULL}/iterations", json={"value": [{"id": 1}]})
    first_page = [
        {"changeType": "edit", "item": {"path": f"/src/f{n}.py", "gitObjectType": "blob"}}
        for n in range(100)
    ]

    def pages(request: httpx.Request) -> httpx.Response:
        if dict(request_query(request))["$skip"] == "0":
            return httpx.Response(200, json={"changeEntries": first_page})
        return never_answers(request)

    ado_transport.route("GET", f"{PULL}/iterations/1/changes", handler=pages)

    result = facet.changed_files(str(tmp_path), reference())

    assert result["has_more"] is True
    assert [file["path"] for file in result["data"]] == [f"src/f{n}.py" for n in range(100)]
    second_page = ado_transport.requests[-1]
    assert dict(request_query(second_page))["$skip"] == "100"
    assert 0 < second_page.extensions["timeout"]["read"] <= seconds


@pytest.mark.parametrize("endpoint", ["iterations", "iterations/1/changes"])
def test_changed_files_failed_before_first_page_are_not_reported_as_empty_success(
    facet: AzureDevOpsPullRequests,
    tmp_path: Path,
    ado_transport: RecordingTransport,
    endpoint: str,
) -> None:
    ado_transport.route("GET", f"{PULL}/iterations", json={"value": [{"id": 1}]})
    fail(ado_transport, f"{PULL}/{endpoint}", 503)

    result = facet.changed_files(str(tmp_path), reference())

    assert result["data"] == []
    assert result["has_more"] is True
    assert result["warning"]


def test_changed_files_page_cap_keeps_loaded_files_and_reports_incompleteness(
    facet: AzureDevOpsPullRequests,
    tmp_path: Path,
    ado_transport: RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(azure_devops_client, "_MAX_CHANGE_PAGES", 1)
    ado_transport.route("GET", f"{PULL}/iterations", json={"value": [{"id": 1}]})
    ado_transport.route(
        "GET",
        f"{PULL}/iterations/1/changes",
        json={
            "changeEntries": [
                {
                    "changeType": "edit",
                    "item": {"path": f"/file{number}.py", "gitObjectType": "blob"},
                }
                for number in range(100)
            ]
        },
    )

    result = facet.changed_files(str(tmp_path), reference())

    assert len(result["data"]) == 100
    assert result["has_more"] is True
    assert result["warning"]


@pytest.mark.parametrize("failure", [503, httpx.ReadTimeout])
def test_pr_diff_reports_provider_failures(
    facet: AzureDevOpsPullRequests,
    workspace: Path,
    ado_transport: RecordingTransport,
    failure: int | type[httpx.TransportError],
) -> None:
    fail(ado_transport, PULL, failure)

    result = facet.pr_diff(str(workspace), reference())

    assert result["patch"] == ""
    assert result["unavailable_reason"] == "lookup_failed"
    assert "could not load" in result["message"]


@pytest.mark.parametrize("patch", [None, ""])
def test_pr_diff_distinguishes_git_failure_from_a_valid_empty_diff(
    facet: AzureDevOpsPullRequests,
    workspace: Path,
    ado_transport: RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
    patch: str | None,
) -> None:
    ado_transport.route("GET", PULL, json=pull_request())
    monkeypatch.setattr(facet, "_ensure_commits", lambda *args: True)
    monkeypatch.setattr(azure_devops_facet, "_merge_base", lambda *args: BASE)
    real_git_out = azure_devops_facet._git_out
    monkeypatch.setattr(
        azure_devops_facet,
        "_git_out",
        lambda root, *args: patch if args[0] == "diff" else real_git_out(root, *args),
    )

    result = facet.pr_diff(str(workspace), reference())

    assert result["patch"] == ""
    assert result.get("unavailable_reason") == ("diff_failed" if patch is None else None)


def test_pr_diff_fetches_the_pr_and_diffs_it_from_the_merge_base(
    facet: AzureDevOpsPullRequests,
    forge: Forge,
    ado_transport: RecordingTransport,
    tmp_path: Path,
) -> None:
    ado_transport.route("GET", PULL, json=pull_request(head=forge.head_sha, base=forge.base_sha))
    assert not has_commit(forge.workspace, forge.head_sha)

    result = facet.pr_diff(str(forge.workspace), reference())

    patch = result["patch"]
    assert result == {"object": "session.github.pr_diff", "patch": patch}
    assert has_commit(forge.workspace, forge.head_sha)
    assert len(re.findall(r"^diff --git ", patch, flags=re.MULTILINE)) == 4
    assert "rename from old_name.py\nrename to new_name.py" in patch
    patch_file = tmp_path / "pr.patch"
    patch_file.write_text(patch)
    git(forge.workspace, "apply", "--check", str(patch_file))


def diffed_files(patch: str) -> int:
    return len(re.findall(r"^diff --git ", patch, flags=re.MULTILINE))


def test_pr_diff_of_a_pr_whose_source_branch_is_gone_reads_the_merge_ref(
    facet: AzureDevOpsPullRequests,
    forge: Forge,
    ado_transport: RecordingTransport,
    fetches: list[tuple[str, float]],
) -> None:
    # Completing the PR deleted its source branch; the merge ref still has its commits.
    git(forge.seed, "checkout", "-b", "merged", "main")
    git(forge.seed, "merge", "--no-ff", "-m", "Merge PR 7", "feature")
    git(forge.seed, "push", "origin", "HEAD:refs/pull/7/merge", ":refs/heads/feature")
    completed = pull_request(7, "completed", head=forge.head_sha, base=forge.base_sha)
    ado_transport.route("GET", PULL, json=completed)

    first = facet.pr_diff(str(forge.workspace), reference())
    again = facet.pr_diff(str(forge.workspace), reference())

    assert diffed_files(first["patch"]) == 4
    assert again == first
    # The base is local, the source branch fetch fails, and the local commits end the fetching.
    assert [ref for ref, _ in fetches] == ["refs/heads/feature", "refs/pull/7/merge"]
    fetch_seconds = azure_devops_facet._FETCH_TIMEOUT_SECONDS
    assert all(0 < timeout <= fetch_seconds for _, timeout in fetches)


def test_pr_diff_fetches_the_target_and_the_source_separately(
    facet: AzureDevOpsPullRequests,
    forge: Forge,
    ado_transport: RecordingTransport,
    fetches: list[tuple[str, float]],
    tmp_path: Path,
) -> None:
    # A new clone has neither commit, and the PR's target branch is gone from the remote.
    clone = tmp_path / "clone"
    clone.mkdir()
    git(clone, "init")
    git(clone, "remote", "add", "origin", ORIGIN)
    git(clone, "config", f"url.{forge.bare}.insteadOf", ORIGIN)
    pr = pull_request(head=forge.head_sha, base=forge.base_sha, targetRefName="refs/heads/gone")
    ado_transport.route("GET", PULL, json=pr)

    result = facet.pr_diff(str(clone), reference())

    # The failed target fetch does not stop the source fetch, which brings both commits.
    assert diffed_files(result["patch"]) == 4
    assert [ref for ref, _ in fetches] == ["refs/heads/gone", "refs/heads/feature"]


def test_pr_diff_does_not_fetch_a_pr_again_for_a_minute_after_fetches_fail(
    facet: AzureDevOpsPullRequests,
    forge: Forge,
    ado_transport: RecordingTransport,
    fetches: list[tuple[str, float]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The source branch is gone and the remote has no merge ref for the PR.
    git(forge.seed, "push", "origin", ":refs/heads/feature")
    abandoned = pull_request(7, "abandoned", head=forge.head_sha, base=forge.base_sha)
    ado_transport.route("GET", PULL, json=abandoned)
    unavailable = UNAVAILABLE_COMMITS

    assert facet.pr_diff(str(forge.workspace), reference()) == unavailable
    assert [ref for ref, _ in fetches] == ["refs/heads/feature", "refs/pull/7/merge"]
    assert facet.pr_diff(str(forge.workspace), reference()) == unavailable
    assert len(fetches) == 2

    monkeypatch.setattr(azure_devops_facet, "_FETCH_RETRY_SECONDS", 0.0)
    assert facet.pr_diff(str(forge.workspace), reference()) == unavailable
    assert len(fetches) == 4


def test_a_fetch_that_did_not_finish_is_tried_again_by_the_next_request(
    facet: AzureDevOpsPullRequests,
    forge: Forge,
    ado_transport: RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ado_transport.route("GET", PULL, json=pull_request(head=forge.head_sha, base=forge.base_sha))
    refs: list[str] = []
    real_git = azure_devops_facet._git

    def git_whose_fetches_time_out(
        root: str, *args: str, **kwargs: Any
    ) -> subprocess.CompletedProcess[bytes] | None:
        if args[0] != "fetch":
            return real_git(root, *args, **kwargs)
        refs.append(args[-1])
        return None

    monkeypatch.setattr(azure_devops_facet, "_git", git_whose_fetches_time_out)

    assert facet.pr_diff(str(forge.workspace), reference()) == UNAVAILABLE_COMMITS
    assert facet.pr_diff(str(forge.workspace), reference()) == UNAVAILABLE_COMMITS
    assert refs == ["refs/heads/feature", "refs/pull/7/merge"] * 2


def test_a_fetch_that_outlasts_the_request_budget_lands_its_commits_for_the_next_request(
    facet: AzureDevOpsPullRequests,
    forge: Forge,
    ado_transport: RecordingTransport,
    held_fetch: HeldFetch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(azure_devops_facet, "_REQUEST_BUDGET_SECONDS", budget(0.5))
    ado_transport.route("GET", PULL, json=pull_request(head=forge.head_sha, base=forge.base_sha))

    assert facet.pr_diff(str(forge.workspace), reference()) == UNAVAILABLE_COMMITS
    held_fetch.release.set()
    assert held_fetch.finished.wait(budget(10))
    result = facet.pr_diff(str(forge.workspace), reference())

    assert diffed_files(result["patch"]) == 4
    assert held_fetch.refs == ["refs/heads/feature"]


def test_concurrent_requests_for_a_pr_start_one_fetch(
    facet: AzureDevOpsPullRequests,
    forge: Forge,
    ado_transport: RecordingTransport,
    held_fetch: HeldFetch,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(azure_devops_facet, "_REQUEST_BUDGET_SECONDS", budget(0.5))
    ado_transport.route("GET", PULL, json=pull_request(head=forge.head_sha, base=forge.base_sha))
    root, pr = str(forge.workspace), reference()

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: facet.pr_diff(root, pr), range(3)))
    held_fetch.release.set()
    assert held_fetch.finished.wait(budget(10))

    assert results == [UNAVAILABLE_COMMITS] * 3
    assert held_fetch.refs == ["refs/heads/feature"]
    assert diffed_files(facet.pr_diff(root, pr)["patch"]) == 4


def test_pr_diff_for_a_pr_outside_the_workspace_remote(
    facet: AzureDevOpsPullRequests, workspace: Path, ado_transport: RecordingTransport
) -> None:
    result = facet.pr_diff(str(workspace), reference(repository="contoso/other/lib"))

    assert result == {
        "object": "session.github.pr_diff",
        "patch": "",
        "unavailable_reason": "pr_outside_workspace",
    }
    assert ado_transport.requests == []


def test_file_diff_reads_local_commits_with_git(
    facet: AzureDevOpsPullRequests, forge: Forge, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", PULL, json=pull_request(head=forge.head_sha, base=forge.base_sha))
    root, pr = str(forge.seed), reference()
    shown = {"head_sha": forge.head_sha, "base_sha": forge.base_sha}

    edited = facet.file_diff(root, pr, "edit.py", base="", previous_path=None, **shown)
    renamed = facet.file_diff(
        root, pr, "new_name.py", base="", previous_path="old_name.py", **shown
    )
    added = facet.file_diff(root, pr, "added.py", base="", previous_path=None, **shown)

    assert edited == {
        "object": "session.github.file_diff",
        "path": "edit.py",
        "before": "one\ntwo\nthree\n",
        "after": "one\n2\nthree\n",
    }
    assert renamed["before"] == renamed["after"] == "moved\ncontent\nhere\n"
    assert (added["before"], added["after"]) == (None, "new\n")
    assert {request_path(request) for request in ado_transport.requests} == {PULL}


def test_file_diff_reads_remote_revisions_through_the_api(
    facet: AzureDevOpsPullRequests, tmp_path: Path, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", PULL, json=pull_request())
    iterations = [
        {"id": 1, "commonRefCommit": {"commitId": "e" * 40}},
        {"id": 2, "commonRefCommit": {"commitId": MERGE_BASE}},
    ]
    ado_transport.route("GET", f"{PULL}/iterations", json={"value": iterations})
    contents = {MERGE_BASE: "old\n", HEAD: "new\n"}

    def items(request: httpx.Request) -> httpx.Response:
        query = dict(request_query(request))
        body = {"path": query["path"], "content": contents[query["versionDescriptor.version"]]}
        return httpx.Response(200, json=body)

    ado_transport.route("GET", f"{REPO_API}/items", handler=items)

    diff = facet.file_diff(
        str(tmp_path),
        reference(),
        "src/app.py",
        base="",
        previous_path="src/old.py",
        head_sha=HEAD,
        base_sha=BASE,
    )

    assert diff == {
        "object": "session.github.file_diff",
        "path": "src/app.py",
        "before": "old\n",
        "after": "new\n",
    }
    reads = [
        dict(request_query(request))
        for request in ado_transport.requests
        if request_path(request) == f"{REPO_API}/items"
    ]
    assert [(read["path"], read["versionDescriptor.version"]) for read in reads] == [
        ("/src/old.py", MERGE_BASE),
        ("/src/app.py", HEAD),
    ]


def test_file_diff_reads_no_revision_after_the_budget_runs_out(
    facet: AzureDevOpsPullRequests,
    tmp_path: Path,
    ado_transport: RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(azure_devops_facet, "_REQUEST_BUDGET_SECONDS", budget(0.5))
    ado_transport.route("GET", PULL, json=pull_request())
    iterations = {"value": [{"id": 1, "commonRefCommit": {"commitId": MERGE_BASE}}]}

    def late_iterations(request: httpx.Request) -> httpx.Response:
        # The answer arrives just after the time left runs out.
        time.sleep(request.extensions["timeout"]["read"] + 0.05)
        return httpx.Response(200, json=iterations)

    ado_transport.route("GET", f"{PULL}/iterations", handler=late_iterations)
    ado_transport.route("GET", f"{REPO_API}/items", json={"content": "text\n"})

    with pytest.raises(ValueError) as excinfo:
        facet.file_diff(
            str(tmp_path),
            reference(),
            "src/app.py",
            base="",
            previous_path=None,
            head_sha=HEAD,
            base_sha=BASE,
        )

    assert str(excinfo.value) == "Azure DevOps could not load the selected file revision"
    assert f"{REPO_API}/items" not in [request_path(request) for request in ado_transport.requests]


def test_file_diff_refuses_a_pr_that_moved(
    facet: AzureDevOpsPullRequests, tmp_path: Path, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", PULL, json=pull_request())

    with pytest.raises(ValueError) as excinfo:
        facet.file_diff(
            str(tmp_path),
            reference(),
            "src/app.py",
            base="",
            previous_path=None,
            head_sha="d" * 40,
            base_sha=BASE,
        )

    assert str(excinfo.value) == "The pull request changed; refresh before expanding context"


@pytest.mark.parametrize("path", ["/etc/passwd", "src/../secret.py", "src//app.py"])
def test_file_diff_rejects_paths_outside_the_repository(
    facet: AzureDevOpsPullRequests, tmp_path: Path, ado_transport: RecordingTransport, path: str
) -> None:
    with pytest.raises(ValueError) as excinfo:
        facet.file_diff(
            str(tmp_path),
            reference(),
            path,
            base="",
            previous_path=None,
            head_sha=None,
            base_sha=None,
        )

    assert str(excinfo.value) == "Invalid repository-relative path"
    assert ado_transport.requests == []


def test_file_diff_without_a_reference_diffs_the_checkout(
    facet: AzureDevOpsPullRequests, forge: Forge, ado_transport: RecordingTransport
) -> None:
    diff = facet.file_diff(
        str(forge.seed),
        None,
        "edit.py",
        base="main",
        previous_path=None,
        head_sha=None,
        base_sha=None,
    )

    assert diff == {
        "object": "session.github.file_diff",
        "path": "edit.py",
        "before": "one\ntwo\nthree\n",
        "after": "one\n2\nthree\n",
    }
    assert ado_transport.requests == []


# ---------------------------------------------------------------------------
# Titles, access, preferences, and the protocol
# ---------------------------------------------------------------------------


def test_pr_title_caps_the_request_timeout_at_the_deadline(
    facet: AzureDevOpsPullRequests, tmp_path: Path, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", PULL, json=pull_request(title="  Add CI \n"))

    assert facet.pr_title(str(tmp_path), reference(), time.monotonic() + 1.5) == ("Add CI", False)

    [request] = ado_transport.requests
    assert 0 < request.extensions["timeout"]["read"] <= 1.5


def test_pr_title_timeout_reports_the_deadline(
    facet: AzureDevOpsPullRequests, tmp_path: Path, ado_transport: RecordingTransport
) -> None:
    def hang(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    ado_transport.route("GET", PULL, handler=hang)

    assert facet.pr_title(str(tmp_path), reference(), time.monotonic() + 1.5) == (None, True)


def test_pr_title_after_the_deadline_sends_nothing(
    facet: AzureDevOpsPullRequests, tmp_path: Path, ado_transport: RecordingTransport
) -> None:
    assert facet.pr_title(str(tmp_path), reference(), time.monotonic() - 1) == (None, True)
    assert ado_transport.requests == []


def test_titles_are_available_with_a_token(
    facet: AzureDevOpsPullRequests, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert facet.titles_available(str(tmp_path)) is True
    monkeypatch.setattr(azure_devops_client, "resolve_token", lambda: None)
    assert facet.titles_available(str(tmp_path)) is False


@pytest.mark.parametrize("status", [None, 401, 403, 404], ids=["no-token", "401", "403", "404"])
def test_verify_accessible_rejects_a_pr_the_host_cannot_read(
    facet: AzureDevOpsPullRequests,
    tmp_path: Path,
    ado_transport: RecordingTransport,
    monkeypatch: pytest.MonkeyPatch,
    status: int | None,
) -> None:
    if status is None:
        monkeypatch.setattr(azure_devops_client, "resolve_token", lambda: None)
    else:
        ado_transport.route("GET", PULL, status=status, json={"message": "TF401180"})

    with pytest.raises(ValueError) as excinfo:
        facet.verify_accessible(str(tmp_path), reference())

    assert str(excinfo.value) == INACCESSIBLE


def test_verify_accessible_accepts_a_readable_pr(
    facet: AzureDevOpsPullRequests, tmp_path: Path, ado_transport: RecordingTransport
) -> None:
    ado_transport.route("GET", PULL, json=pull_request())

    facet.verify_accessible(str(tmp_path), reference())

    assert [request_path(request) for request in ado_transport.requests] == [PULL]


def test_set_preference_rejects_account_and_remote_choices(
    facet: AzureDevOpsPullRequests, tmp_path: Path
) -> None:
    with pytest.raises(ValueError, match="no account or base remote choice"):
        facet.set_preference(str(tmp_path), None, account="alice", remote=None)
    with pytest.raises(ValueError, match="no account or base remote choice"):
        facet.set_preference(str(tmp_path), reference(), account=None, remote="upstream")
    facet.set_preference(str(tmp_path), None, account=None, remote=None)


def test_module_instance_handles_unsupported_actions(tmp_path: Path) -> None:
    assert PULL_REQUESTS.capabilities.to_json() == NO_CAPABILITIES
    assert PULL_REQUESTS.shell_pr_operations([]) == []
    assert PULL_REQUESTS.pr_from_object({"url": PR_URL}) is None
    assert PULL_REQUESTS.mcp_prs("create_pull_request", {}, {}) is None
    PULL_REQUESTS.on_inferred_pr(str(tmp_path), reference())


def test_importing_the_facet_loads_neither_httpx_nor_the_rest_client() -> None:
    """The observer imports every facet on each tool call, so the REST client loads lazily.

    Runs in a fresh interpreter so modules other tests imported cannot hide an import.
    """
    probe = (
        "import sys\n"
        "import omnigent.runner.git_providers.azure_devops\n"
        "loaded = {'httpx', 'omnigent.runner.azure_devops_client'} & set(sys.modules)\n"
        "assert not loaded, sorted(loaded)\n"
    )
    child_env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}

    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=budget(120),
    )

    assert result.returncode == 0, result.stderr
