"""Azure DevOps pull request facet for the session PR panel.

PR metadata, checks, comments, and changed files come from the Azure DevOps REST
API through :mod:`omnigent.runner.azure_devops_client`, which contacts only
``dev.azure.com``. The whole-PR diff and file contents come from local git when
the PR's commits are in the workspace. An info or whole-PR diff request stays
within one time budget, under the server's limit for proxied runner reads. The
observer imports this module on every tool completion, so the REST client and
``httpx`` load inside the functions that use them.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, ParamSpec, TypeVar
from urllib.parse import quote

from omnigent.git_providers.azure_devops import (
    AzureRepo,
    canonical_pr_url,
    parse_azure_devops_remote,
)
from omnigent.runner.git_providers import (
    CHANGED_FILE_OBJECT,
    FILE_DIFF_OBJECT,
    INFO_OBJECT,
    PR_DIFF_OBJECT,
    PR_OUTSIDE_WORKSPACE,
    ProviderCapabilities,
    PullRequestAuth,
    PullRequestFacet,
    ShellPrOp,
    ShellSegment,
    azure_devops_observer,
    local_git,
)
from omnigent.runner.session_prs import PullRequestRef

if TYPE_CHECKING:
    import httpx

    from omnigent.runner.azure_devops_client import AzureDevOpsClient, AzureToken

_logger = logging.getLogger(__name__)

_P = ParamSpec("_P")
_T = TypeVar("_T")

_PROVIDER_ID = "azure_devops"
_CAPABILITIES = ProviderCapabilities(
    account_switching=False,
    base_remote_selection=False,
    line_counts=False,
    linked_pr_diff=False,
)
_AUTH_HINT = "Run az login on the host or set AZURE_DEVOPS_EXT_PAT."
_INACCESSIBLE = "Cannot access this pull request with the Azure DevOps credentials on the host"
_PR_LOAD_FAILED = "Azure DevOps could not load the selected PR's file content"
_UNEXPECTED_RESPONSE = "Azure DevOps returned an unexpected file response"
_REVISION_LOAD_FAILED = "Azure DevOps could not load the selected file revision"
_NO_CONTEXT = "Expanded context is unavailable for this file"
_GIT_TIMEOUT_SECONDS = 30.0
# One panel request's time budget, under the runner proxy's ten-second limit.
_REQUEST_BUDGET_SECONDS = 8.0
# A PR's fetch runs in the background for up to this long, past the request that started it.
_FETCH_TIMEOUT_SECONDS = 120.0
# A PR whose fetch failed is not fetched again for this long.
_FETCH_RETRY_SECONDS = 60.0
# The GitHub panel's caps; the check counts stay exact.
_MAX_CHECK_RUNS = 300
_MAX_COMMENTS = 100
# Commit ids reach git as arguments, so only full SHA-1 ids are used.
_COMMIT_ID = re.compile(r"[0-9a-fA-F]{40}")
_DENIED_STATUSES = frozenset({401, 403, 404})
_PR_STATES = {"active": "OPEN", "completed": "MERGED", "abandoned": "CLOSED"}
_STATUS_BUCKETS = {
    "succeeded": "passing",
    "failed": "failing",
    "error": "failing",
    "pending": "pending",
    "notset": "pending",
}
_EVALUATION_BUCKETS = {
    "approved": "passing",
    "rejected": "failing",
    "broken": "failing",
    "queued": "pending",
    "running": "pending",
}
_NOT_APPLICABLE = "notapplicable"
_BUILD_POLICY_TYPE = "0609b952-1397-4640-95ec-e00a01b2c241"
# The panel runs git unattended, so neither git nor Git Credential Manager may prompt.
_NO_PROMPT_ENV = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}


# ── Local git ─────────────────────────────────────────────────────────────────


def _git(
    root: str, *args: str, timeout: float = _GIT_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess[bytes] | None:
    """Run ``git -C root``, or return ``None`` when git cannot start or times out."""
    try:
        return subprocess.run(
            ["git", "-C", root, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            env={**os.environ, **_NO_PROMPT_ENV},
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        _logger.debug("azure_devops: git %s did not finish: %s", args[0], type(exc).__name__)
        return None


def _text_run(root: str) -> local_git.TextRun:
    """Run git commands in ``root`` for :mod:`local_git`, decoding the output leniently."""

    def run(argv: list[str]) -> tuple[int | None, str]:
        result = _git(root, *argv)
        if result is None:
            return None, ""
        return result.returncode, result.stdout.decode("utf-8", errors="replace")

    return run


def _bytes_run(root: str) -> Callable[[list[str]], tuple[int | None, bytes]]:
    """Run git commands in ``root`` for :mod:`local_git`, keeping the output as bytes."""

    def run(argv: list[str]) -> tuple[int | None, bytes]:
        result = _git(root, *argv)
        return (None, b"") if result is None else (result.returncode, result.stdout)

    return run


def _git_out(root: str, *args: str) -> str | None:
    """Return the output of a git command that succeeded, or ``None``."""
    rc, out = _text_run(root)(list(args))
    return out if rc == 0 else None


def _branch(root: str) -> str | None:
    """Return the checked-out branch (``HEAD`` when detached), or ``None`` outside a checkout."""
    out = _git_out(root, "rev-parse", "--abbrev-ref", "HEAD")
    return None if out is None else out.strip()


def _remotes(root: str) -> list[tuple[str, AzureRepo]]:
    """Return the workspace's Azure DevOps remotes as ``(name, repo)``, ``origin`` first."""
    return [
        (name, repo)
        for name, url in local_git.remote_urls(_text_run(root))
        if (repo := parse_azure_devops_remote(url)) is not None
    ]


def _has_commits(root: str, *commits: str) -> bool:
    """Return whether every commit is in the workspace's object store."""
    return all(
        _git_out(root, "cat-file", "-e", f"{commit}^{{commit}}") is not None for commit in commits
    )


def _merge_base(root: str, base: str, head: str) -> str | None:
    """Return the merge base of two local commits, or ``None``."""
    out = _git_out(root, "merge-base", base, head)
    return (out or "").strip() or None


def _fetch(root: str, remote: str, ref: str, deadline: float) -> int | None:
    """Fetch one ref from ``remote`` before ``deadline``; a failure is logged.

    :returns: git's exit status, or ``None`` when the fetch did not start or did not finish.
    """
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return None
    result = _git(root, "fetch", "--no-tags", "--quiet", remote, ref, timeout=remaining)
    if result is None or result.returncode != 0:
        _logger.info("azure_devops: could not fetch %s from %s", ref, remote)
    return None if result is None else result.returncode


def _fetch_pr(
    root: str, remote: str, number: int, pr: dict[str, Any], head: str, base: str
) -> bool:
    """Fetch the PR's target branch, source branch, and merge ref, each while a commit is missing.

    :returns: Whether the fetch failed: a fetch exited non-zero and a commit is still missing.
    """
    deadline = time.monotonic() + _FETCH_TIMEOUT_SECONDS
    statuses: list[int | None] = []
    target = _branch_ref(pr, "targetRefName")
    if target is not None and not _has_commits(root, base):
        statuses.append(_fetch(root, remote, target, deadline))
    source = _branch_ref(pr, "sourceRefName")
    if source is not None and not _has_commits(root, head):
        statuses.append(_fetch(root, remote, source, deadline))
    if not _has_commits(root, head, base):
        # Completing a PR often deletes its source branch. Azure Repos keeps a merge ref
        # while the PR exists, whose parents are the PR's target and source commits.
        statuses.append(_fetch(root, remote, f"refs/pull/{number}/merge", deadline))
    exited_non_zero = any(status not in (None, 0) for status in statuses)
    return exited_non_zero and not _has_commits(root, head, base)


_FetchKey = tuple[str, str, int]


class _PrFetches:
    """Background fetches of PR commits, one at a time per ``(workspace, remote, PR id)``.

    A request waits for its PR's fetch only while its own budget lasts, and the fetch runs on
    so that a later request finds the commits. After a failed fetch, the PR is not fetched
    again for :data:`_FETCH_RETRY_SECONDS`. Safe to use from several threads.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._running: dict[_FetchKey, threading.Event] = {}
        self._failed_at: dict[_FetchKey, float] = {}

    def wait(self, key: _FetchKey, fetch: Callable[[], bool], deadline: float) -> bool:
        """Start ``fetch`` for ``key`` unless one is running or failed recently, and wait for it.

        :param fetch: Fetches the PR's commits and returns whether the fetch failed.
        :param deadline: A ``time.monotonic()`` value that ends the wait, not the fetch.
        :returns: Whether a fetch for ``key`` finished before ``deadline``.
        """
        starter: threading.Thread | None = None
        with self._lock:
            failed_at = self._failed_at.get(key)
            if failed_at is not None and time.monotonic() - failed_at < _FETCH_RETRY_SECONDS:
                return False
            done = self._running.get(key)
            if done is None:
                done = self._running[key] = threading.Event()
                starter = threading.Thread(
                    target=self._run,
                    args=(key, fetch, done),
                    name="azure-devops-fetch",
                    daemon=True,
                )
        if starter is not None:
            starter.start()
        return done.wait(max(deadline - time.monotonic(), 0.0))

    def _run(self, key: _FetchKey, fetch: Callable[[], bool], done: threading.Event) -> None:
        """Run ``fetch``, record whether it failed, and wake the requests waiting for it."""
        failed = False
        try:
            failed = fetch()
        finally:
            now = time.monotonic()
            with self._lock:
                del self._running[key]
                self._failed_at = {
                    other: failed_at
                    for other, failed_at in self._failed_at.items()
                    if now - failed_at < _FETCH_RETRY_SECONDS
                }
                if failed:
                    self._failed_at[key] = now
            done.set()


def _pr_text(root: str, ref: str, path: str) -> str | None:
    """Read a PR file at a local revision; binary content has no expanded context."""
    data = local_git.read_file(_bytes_run(root), ref, path)
    if data is None:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(_NO_CONTEXT) from exc
    if "\x00" in text:
        raise ValueError(_NO_CONTEXT)
    return text


# ── REST ──────────────────────────────────────────────────────────────────────


class _RestFailure(Exception):
    """A request to Azure DevOps failed.

    :ivar denied: The credentials cannot read the resource.
    :ivar timed_out: The request ran out of time.
    """

    def __init__(self, *, denied: bool = False, timed_out: bool = False) -> None:
        super().__init__("Azure DevOps request failed")
        self.denied = denied
        self.timed_out = timed_out


def _call(read: Callable[_P, _T], *args: _P.args, **kwargs: _P.kwargs) -> _T:
    """Run one client call, raising any client or transport error as :class:`_RestFailure`."""
    import httpx

    from omnigent.runner.azure_devops_client import AzureDevOpsError

    try:
        return read(*args, **kwargs)
    except AzureDevOpsError as exc:
        _logger.debug("azure_devops: %s", exc)
        # A failed 2xx is a non-JSON body, such as the sign-in page a rejected credential gets.
        denied = exc.status in _DENIED_STATUSES or 200 <= exc.status < 300
        raise _RestFailure(denied=denied) from exc
    except httpx.TimeoutException as exc:
        raise _RestFailure(timed_out=True) from exc
    except httpx.HTTPError as exc:
        _logger.debug("azure_devops: request failed: %s", type(exc).__name__)
        raise _RestFailure() from exc


def _optional(
    read: Callable[_P, list[dict[str, Any]]], *args: _P.args, **kwargs: _P.kwargs
) -> list[dict[str, Any]] | None:
    """Run a list call, keeping failures distinct from successful empty results."""
    try:
        return _call(read, *args, **kwargs)
    except _RestFailure:
        return None


def _token() -> AzureToken | None:
    """Return the host's Azure DevOps credential, or ``None``."""
    from omnigent.runner.azure_devops_client import resolve_token

    return resolve_token()


def _auth(authenticated: bool) -> PullRequestAuth:
    """Return the ``auth`` block; ``az`` also counts when found at the Homebrew paths."""
    from omnigent.runner.azure_devops_client import _find_az

    return {
        "authenticated": authenticated,
        "hint": _AUTH_HINT,
        "cli": {"name": "az", "available": _find_az() is not None},
        "accounts": None,
        "selected_account": None,
    }


def _info(*, authenticated: bool, **fields: Any) -> dict[str, Any]:
    """Return an available info payload with the provider fields and ``fields``."""
    return {
        "object": INFO_OBJECT,
        "available": True,
        "provider": _PROVIDER_ID,
        "auth": _auth(authenticated),
        "capabilities": _CAPABILITIES.to_json(),
        **fields,
    }


# ── Payload shaping ───────────────────────────────────────────────────────────


def _nested(value: object, *keys: str) -> Any:
    """Follow ``keys`` through nested JSON objects; ``None`` when a level is missing."""
    for key in keys:
        if not isinstance(value, dict):
            return None
        value = value.get(key)
    return value


def _text(value: object) -> str | None:
    """Return ``value`` when it is a non-empty string."""
    return value if isinstance(value, str) and value else None


def _number(value: object) -> int | None:
    """Return ``value`` when it is an integer and not a bool."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _commit(value: object, key: str) -> str | None:
    """Return ``value[key].commitId`` when it is a full commit id."""
    commit = _nested(value, key, "commitId")
    return commit if isinstance(commit, str) and _COMMIT_ID.fullmatch(commit) else None


def _short_ref(ref: object) -> str | None:
    """Return a branch ref without its ``refs/heads/`` prefix."""
    return ref.removeprefix("refs/heads/") if isinstance(ref, str) and ref else None


def _branch_ref(pr: dict[str, Any], key: str) -> str | None:
    """Return the PR's ref under ``key`` when it is a ``refs/heads/`` branch ref."""
    ref = pr.get(key)
    return ref if isinstance(ref, str) and ref.startswith("refs/heads/") else None


def _reference_repo(reference: PullRequestRef) -> AzureRepo | None:
    """Return the repository of a tracked PR, whose ``repository`` is ``org/project/repo``."""
    parts = reference.repository.split("/")
    if len(parts) != 3 or not all(parts):
        return None
    org, project, repo = parts
    return AzureRepo(org, project, repo)


def _target_repo(root: str, reference: PullRequestRef | None) -> AzureRepo | None:
    """Return the reference's repository, or the workspace's for its branch PR."""
    if reference is not None:
        return _reference_repo(reference)
    remotes = _remotes(root)
    return remotes[0][1] if remotes else None


def _name(repo: AzureRepo) -> str:
    """Return ``org/project/repo``, the panel's ``repo.name_with_owner``."""
    return f"{repo.org}/{repo.project}/{repo.repo}"


def _branch_pr(
    client: AzureDevOpsClient, repo: AzureRepo, branch: str | None
) -> dict[str, Any] | None:
    """Return the branch's PR from the PR list: the newest active one, else the newest.

    PR ids grow over time, so the highest id is the newest PR.

    :raises _RestFailure: When the list cannot be read.
    """
    if branch is None or branch == "HEAD":
        return None
    found = _call(client.find_pull_requests, repo.project, repo.repo, branch)
    prs = [
        pr for pr in found if isinstance(pr, dict) and _number(pr.get("pullRequestId")) is not None
    ]
    active = [pr for pr in prs if pr.get("status") == "active"]
    return max(active or prs, key=lambda pr: pr["pullRequestId"], default=None)


def _pr_number(
    client: AzureDevOpsClient, root: str, repo: AzureRepo, reference: PullRequestRef | None
) -> int | None:
    """Return the reference's PR id, or the id of the checked-out branch's PR.

    :raises _RestFailure: When the branch's PRs cannot be listed.
    """
    if reference is not None:
        return reference.number
    listed = _branch_pr(client, repo, _branch(root))
    return None if listed is None else listed["pullRequestId"]


def _latest_iteration(iterations: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the iteration with the highest id, the PR's latest push."""
    numbered = [
        it for it in iterations if isinstance(it, dict) and _number(it.get("id")) is not None
    ]
    return max(numbered, key=lambda it: it["id"], default=None)


def _status_runs(statuses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Bucket the latest status of each ``genre/name`` context, dropping not-applicable ones."""
    latest: dict[tuple[str, str], tuple[tuple[int, int], dict[str, Any]]] = {}
    for index, status in enumerate(statuses):
        if not isinstance(status, dict):
            continue
        genre = _text(_nested(status, "context", "genre")) or ""
        name = _text(_nested(status, "context", "name")) or ""
        rank = (_number(status.get("id")) or 0, index)
        if (genre, name) not in latest or rank > latest[(genre, name)][0]:
            latest[(genre, name)] = (rank, status)
    runs: list[dict[str, Any]] = []
    for (genre, name), (_, status) in latest.items():
        state = str(status.get("state") or "").lower()
        if state == _NOT_APPLICABLE:
            continue
        runs.append(
            {
                "name": "/".join(part for part in (genre, name) if part) or "check",
                "bucket": _STATUS_BUCKETS.get(state, "pending"),
                "url": _text(status.get("targetUrl")),
            }
        )
    return runs


def _is_build_policy(evaluation: dict[str, Any]) -> bool:
    """Return whether a policy evaluation belongs to a build validation policy."""
    policy_type = _nested(evaluation, "configuration", "type")
    return _nested(policy_type, "id") == _BUILD_POLICY_TYPE or (
        str(_nested(policy_type, "displayName") or "").lower() == "build"
    )


def _evaluation_runs(evaluations: list[dict[str, Any]], repo: AzureRepo) -> list[dict[str, Any]]:
    """Bucket the build policy evaluations, dropping not-applicable ones."""
    runs: list[dict[str, Any]] = []
    for evaluation in evaluations:
        if not isinstance(evaluation, dict) or not _is_build_policy(evaluation):
            continue
        status = str(evaluation.get("status") or "").lower()
        if status == _NOT_APPLICABLE:
            continue
        build_id = _number(_nested(evaluation, "context", "buildId"))
        url = None
        if build_id is not None:
            org, project = (quote(name, safe="") for name in (repo.org, repo.project))
            url = f"https://dev.azure.com/{org}/{project}/_build/results?buildId={build_id}"
        runs.append(
            {
                "name": _text(_nested(evaluation, "configuration", "settings", "displayName"))
                or _text(_nested(evaluation, "context", "buildDefinitionName"))
                or "Build",
                "bucket": _EVALUATION_BUCKETS.get(status, "pending"),
                "url": url,
            }
        )
    return runs


def _summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Return the ``checks`` block: exact bucket counts and the capped list of runs."""
    counts = {"passing": 0, "failing": 0, "pending": 0}
    for run in runs:
        counts[run["bucket"]] += 1
    return {**counts, "total": sum(counts.values()), "runs": runs[:_MAX_CHECK_RUNS]}


def _inline_location(context: object) -> str | None:
    """Return ``path:line`` for a thread on a file, or ``None`` for a PR-level thread."""
    path = _text(_nested(context, "filePath"))
    if path is None:
        return None
    # A comment on a removed line has only a left-side position.
    line = _number(_nested(context, "rightFileStart", "line")) or _number(
        _nested(context, "leftFileStart", "line")
    )
    location = path.removeprefix("/")
    return f"{location}:{line}" if line else location


def _comments(threads: list[dict[str, Any]], pr_url: str) -> list[dict[str, Any]]:
    """Shape the threads' comments, skipping deleted and system ones, capped at 100."""
    shaped: list[dict[str, Any]] = []
    for thread in threads:
        if not isinstance(thread, dict) or thread.get("isDeleted"):
            continue
        location = _inline_location(thread.get("threadContext"))
        thread_id = _number(thread.get("id"))
        url = None if thread_id is None else f"{pr_url}?discussionId={thread_id}"
        comments = thread.get("comments")
        for comment in comments if isinstance(comments, list) else []:
            if (
                not isinstance(comment, dict)
                or comment.get("isDeleted")
                or comment.get("commentType") == "system"
            ):
                continue
            body = str(comment.get("content") or "")
            shaped.append(
                {
                    "author": _text(_nested(comment, "author", "displayName")),
                    "author_id": _text(_nested(comment, "author", "id")),
                    "body": f"`{location}`\n\n{body}" if location else body,
                    "created_at": _text(comment.get("publishedDate")),
                    "url": url,
                }
            )
            if len(shaped) > _MAX_COMMENTS:
                return shaped
    return shaped


def _evaluations(
    client: AzureDevOpsClient, repo: AzureRepo, pr: dict[str, Any], number: int
) -> list[dict[str, Any]] | None:
    """Read the PR's policy evaluations, which need the project id that ``pr`` carries."""
    project_id = _text(_nested(pr, "repository", "project", "id"))
    if project_id is None:
        return None
    return _optional(client.policy_evaluations, repo.project, project_id, number)


def _pr_or_listed(
    client: AzureDevOpsClient, repo: AzureRepo, number: int, listed: dict[str, Any]
) -> tuple[dict[str, Any], bool]:
    """Read a listed PR for its full description, which the PR list truncates.

    When the read fails without a denial, the list entry stands in so the PR stays shown.

    :raises _RestFailure: When the credentials are denied.
    """
    try:
        return _call(client.get_pull_request, repo.project, repo.repo, number), False
    except _RestFailure as failure:
        if failure.denied:
            raise
        return listed, True


def _pr_payload(
    client: AzureDevOpsClient,
    repo: AzureRepo,
    number: int,
    url: str,
    listed: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], bool]:
    """Read a PR with its statuses, policy evaluations, and threads; return the payload's ``pr``.

    The three lists load at once, and one that cannot be read is marked incomplete. With
    ``listed``, the PR's list entry, the PR loads alongside them; without it, they wait for
    the PR, which carries the project id that the evaluations need.

    :raises _RestFailure: When the PR cannot be read and no list entry stands in for it.
    """
    project, name = repo.project, repo.repo
    pr = listed if listed is not None else _call(client.get_pull_request, project, name, number)
    details_partial = False
    with ThreadPoolExecutor(max_workers=4) as pool:
        full = None if listed is None else pool.submit(_pr_or_listed, client, repo, number, listed)
        statuses = pool.submit(_optional, client.statuses, project, name, number)
        evaluations = pool.submit(_evaluations, client, repo, pr, number)
        threads = pool.submit(_optional, client.threads, project, name, number)
        if full is not None:
            pr, details_partial = full.result()
    status_rows, policy_rows, thread_rows = (
        statuses.result(),
        evaluations.result(),
        threads.result(),
    )
    runs = [*_status_runs(status_rows or []), *_evaluation_runs(policy_rows or [], repo)]
    checks = _summarize(runs)
    if status_rows is None or policy_rows is None:
        checks["partial"] = True
    comments = _comments(thread_rows or [], url)
    description = pr.get("description")
    payload = {
        "number": number,
        "url": url,
        "title": pr.get("title"),
        "state": _PR_STATES.get(str(pr.get("status") or "").lower(), "OPEN"),
        "is_draft": pr.get("isDraft") is True,
        "author": _text(_nested(pr, "createdBy", "displayName")),
        "author_id": _text(_nested(pr, "createdBy", "id")),
        "base_ref": _short_ref(pr.get("targetRefName")),
        "head_ref": _short_ref(pr.get("sourceRefName")),
        "head_sha": _commit(pr, "lastMergeSourceCommit"),
        "base_sha": _commit(pr, "lastMergeTargetCommit"),
        "checks": checks,
        "body": description if isinstance(description, str) and description.strip() else None,
        "comments": comments[:_MAX_COMMENTS],
    }
    if thread_rows is None or len(comments) > _MAX_COMMENTS:
        payload["comments_partial"] = True
    return payload, details_partial


def _changed_file(change: object) -> dict[str, Any] | None:
    """Shape one iteration change entry; ``None`` for a folder or an entry without a path."""
    item = _nested(change, "item")
    path = (_text(_nested(item, "path")) or "").removeprefix("/")
    if not path or _nested(item, "isFolder") is True or _nested(item, "gitObjectType") == "tree":
        return None
    change_type = _nested(change, "changeType")
    flags = (
        {flag.strip().lower() for flag in change_type.split(",")}
        if isinstance(change_type, str)
        else set()
    )
    # A combined change such as ``edit, rename`` is a rename.
    if "rename" in flags:
        status = "renamed"
    elif "add" in flags:
        status = "created"
    elif "delete" in flags:
        status = "deleted"
    else:
        status = "modified"
    return {
        "object": CHANGED_FILE_OBJECT,
        "path": path,
        "name": path.split("/")[-1],
        "status": status,
        "lines_added": None,
        "lines_removed": None,
    }


# ── Facet ─────────────────────────────────────────────────────────────────────


def _unavailable_diff(reason: str, message: str) -> dict[str, Any]:
    return {
        "object": PR_DIFF_OBJECT,
        "patch": "",
        "unavailable_reason": reason,
        "message": message,
    }


class AzureDevOpsPullRequests:
    """The session PR panel for repositories on Azure DevOps Services.

    :param transport: HTTP transport for the REST client, such as an
        :class:`httpx.MockTransport` in tests. ``None`` uses the network.
    """

    def __init__(self, *, transport: httpx.BaseTransport | None = None) -> None:
        self._transport = transport
        self._fetches = _PrFetches()

    @property
    def capabilities(self) -> ProviderCapabilities:
        """Every optional panel feature is off."""
        return _CAPABILITIES

    def _client(
        self, org: str, token: AzureToken, *, deadline: float | None = None
    ) -> AzureDevOpsClient:
        from omnigent.runner.azure_devops_client import AzureDevOpsClient

        return AzureDevOpsClient(org, token, deadline=deadline, transport=self._transport)

    def workspace_info(self, root: str) -> dict[str, Any]:
        """Return the checked-out branch, the workspace's Azure DevOps repository, and its PR."""
        deadline = time.monotonic() + _REQUEST_BUDGET_SECONDS
        branch = _branch(root)
        if branch is None:
            return {
                "object": INFO_OBJECT,
                "available": False,
                "reason": "not_a_git_repo",
                "provider": _PROVIDER_ID,
                "auth": None,
                "capabilities": _CAPABILITIES.to_json(),
            }
        remotes = _remotes(root)
        repo = remotes[0][1] if remotes else None
        token = _token()
        info = _info(
            authenticated=token is not None,
            branch=branch,
            base_ref=None,
            repo=None if repo is None else {"name_with_owner": _name(repo)},
            pr=None,
        )
        if repo is None or token is None:
            return info
        try:
            with self._client(repo.org, token, deadline=deadline) as client:
                listed = _branch_pr(client, repo, branch)
                if listed is None:
                    return info
                number = listed["pullRequestId"]
                url = canonical_pr_url(repo.org, repo.project, repo.repo, number)
                info["pr"], details_partial = _pr_payload(client, repo, number, url, listed)
                if details_partial:
                    info["warnings"] = [
                        "Some pull request details could not be loaded from Azure DevOps."
                    ]
        except _RestFailure as failure:
            info["auth"]["authenticated"] = not failure.denied
            info["warnings"] = ["Azure DevOps could not load pull request information."]
            return info
        info["base_ref"] = info["pr"]["base_ref"]
        return info

    def reference_info(
        self,
        root: str,  # noqa: ARG002 - the PR is read from Azure DevOps, not the checkout
        reference: PullRequestRef,
    ) -> dict[str, Any]:
        """Return the payload for one tracked PR, read by id from Azure DevOps."""
        deadline = time.monotonic() + _REQUEST_BUDGET_SECONDS
        info = _info(
            authenticated=False,
            branch=None,
            base_ref=None,
            repo={"name_with_owner": reference.repository},
            pr=None,
            selected_pr_url=reference.url,
        )
        repo = _reference_repo(reference)
        token = _token() if repo is not None else None
        if repo is None or token is None:
            return info
        try:
            with self._client(repo.org, token, deadline=deadline) as client:
                payload, _ = _pr_payload(client, repo, reference.number, reference.url)
        except _RestFailure as failure:
            # Only a denied request means signed out; a timeout or server error does not.
            info["auth"]["authenticated"] = not failure.denied
            info["warnings"] = ["Azure DevOps could not load pull request information."]
            return info
        info["auth"]["authenticated"] = True
        info.update(branch=payload["head_ref"], base_ref=payload["base_ref"], pr=payload)
        return info

    def titles_available(self, root: str) -> bool:  # noqa: ARG002 - credentials are per host
        """Return whether the host has an Azure DevOps credential."""
        return _token() is not None

    def pr_title(
        self,
        root: str,  # noqa: ARG002 - the title is read from Azure DevOps
        reference: PullRequestRef,
        deadline: float,
    ) -> tuple[str | None, bool]:
        """Read one PR's title; the request timeout is the time left before ``deadline``."""
        repo = _reference_repo(reference)
        if repo is None:
            return None, False
        if time.monotonic() >= deadline:
            return None, True
        token = _token()
        if token is None:
            return None, False
        try:
            with self._client(repo.org, token, deadline=deadline) as client:
                pr = _call(client.get_pull_request, repo.project, repo.repo, reference.number)
        except _RestFailure as failure:
            # The request timeout is the time left, so a timeout means the deadline passed.
            return None, failure.timed_out
        title = pr.get("title")
        return (title.strip() or None) if isinstance(title, str) else None, False

    def verify_accessible(
        self,
        root: str,  # noqa: ARG002 - the PR is read from Azure DevOps
        reference: PullRequestRef,
    ) -> None:
        """Read the PR with the host's credential before it is attached.

        :raises ValueError: When there is no credential or the PR cannot be read.
        """
        repo = _reference_repo(reference)
        token = _token() if repo is not None else None
        if repo is None or token is None:
            raise ValueError(_INACCESSIBLE)
        try:
            with self._client(repo.org, token) as client:
                _call(client.get_pull_request, repo.project, repo.repo, reference.number)
        except _RestFailure as exc:
            raise ValueError(_INACCESSIBLE) from exc

    def on_inferred_pr(self, root: str, reference: PullRequestRef) -> None:
        """Do nothing: Azure DevOps has no per-PR preference to copy."""

    def changed_files(self, root: str, reference: PullRequestRef | None) -> dict[str, Any]:
        """List the files of the PR's latest iteration, compared with the merge base.

        When a page of changes cannot be read, such as when the request budget runs out,
        the files already read come back with ``has_more`` set.
        """
        deadline = time.monotonic() + _REQUEST_BUDGET_SECONDS
        empty: dict[str, Any] = {"object": "list", "data": [], "has_more": False}
        repo = _target_repo(root, reference)
        token = _token() if repo is not None else None
        if repo is None or token is None:
            return {
                **empty,
                "has_more": True,
                "warning": "Check Azure DevOps credentials on the host to load changed files.",
            }
        changes: list[dict[str, Any]] = []
        complete = True
        try:
            with self._client(repo.org, token, deadline=deadline) as client:
                number = _pr_number(client, root, repo, reference)
                if number is None:
                    return empty
                iterations = _call(client.iterations, repo.project, repo.repo, number)
                latest = _latest_iteration(iterations)
                if latest is None:
                    return {
                        **empty,
                        "has_more": True,
                        "warning": "Azure DevOps file changes are not ready. Try again shortly.",
                    }
                pages = client.iteration_change_pages(
                    repo.project, repo.repo, number, latest["id"], compare_to=0
                )
                while (page := _call(lambda: next(pages, None))) is not None:
                    changes.extend(page)
        except _RestFailure:
            complete = False
        files = [shaped for change in changes if (shaped := _changed_file(change)) is not None]
        result = {"object": "list", "data": files, "has_more": not complete}
        if not complete:
            result["warning"] = "Azure DevOps could not load every changed file."
        return result

    def pr_diff(self, root: str, reference: PullRequestRef | None) -> dict[str, Any]:
        """Diff the PR's head against its merge base in the workspace, fetching missing commits."""
        deadline = time.monotonic() + _REQUEST_BUDGET_SECONDS
        empty: dict[str, Any] = {"object": PR_DIFF_OBJECT, "patch": ""}
        remotes = _remotes(root)
        if reference is None:
            if not remotes:
                return empty
            remote, repo = remotes[0]
        else:
            wanted = reference.repository.lower()
            match = next((pair for pair in remotes if _name(pair[1]).lower() == wanted), None)
            if match is None:
                return {**empty, "unavailable_reason": PR_OUTSIDE_WORKSPACE}
            remote, repo = match
        token = _token()
        if token is None:
            return _unavailable_diff("authentication_required", _AUTH_HINT)
        try:
            with self._client(repo.org, token, deadline=deadline) as client:
                number = _pr_number(client, root, repo, reference)
                if number is None:
                    return empty
                pr = _call(client.get_pull_request, repo.project, repo.repo, number)
        except _RestFailure as failure:
            return _unavailable_diff(
                "lookup_failed",
                _INACCESSIBLE
                if failure.denied
                else "Azure DevOps could not load the pull request diff. Try again shortly.",
            )
        head = _commit(pr, "lastMergeSourceCommit")
        base = _commit(pr, "lastMergeTargetCommit")
        if head is None or base is None:
            return _unavailable_diff(
                "revisions_unavailable",
                "Azure DevOps has no revisions ready for this pull request. Try again shortly.",
            )
        if not self._ensure_commits(root, remote, number, pr, (head, base), deadline):
            return _unavailable_diff(
                "commits_unavailable",
                "Commits are unavailable locally. Refresh after the background fetch.",
            )
        merge_base = _merge_base(root, base, head)
        if merge_base is None:
            return _unavailable_diff(
                "merge_base_unavailable", "The pull request's merge base could not be resolved."
            )
        patch = _git_out(
            root,
            "diff",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--find-renames",
            # Fixed prefixes keep the patch parseable whatever diff.noPrefix says.
            "--src-prefix=a/",
            "--dst-prefix=b/",
            merge_base,
            head,
        )
        if patch is None:
            return _unavailable_diff(
                "diff_failed", "Git could not produce this pull request's diff."
            )
        return {"object": PR_DIFF_OBJECT, "patch": patch}

    def _ensure_commits(
        self,
        root: str,
        remote: str,
        number: int,
        pr: dict[str, Any],
        commits: tuple[str, str],
        deadline: float,
    ) -> bool:
        """Return whether the PR's ``(head, base)`` commits are local, fetching missing ones.

        The fetch runs in the background and is waited for until ``deadline``. A fetch that
        outlasts the wait keeps running, so a later request finds the commits.
        """
        head, base = commits
        if _has_commits(root, head, base):
            return True
        finished = self._fetches.wait(
            (root, remote, number),
            lambda: _fetch_pr(root, remote, number, pr, head, base),
            deadline,
        )
        return finished and _has_commits(root, head, base)

    def file_diff(
        self,
        root: str,
        reference: PullRequestRef | None,
        path: str,
        *,
        base: str,
        previous_path: str | None,
        head_sha: str | None,
        base_sha: str | None,
    ) -> dict[str, Any]:
        """Return one file's content at the PR's merge base and head.

        Reads local commits with git and falls back to the REST API. Without a
        ``reference``, diffs the local checkout against ``base``.

        :raises ValueError: When the path is invalid, the PR moved past the shown
            revisions, or the content cannot be read within the request budget.
        """
        deadline = time.monotonic() + _REQUEST_BUDGET_SECONDS
        if reference is None:
            return self._checkout_file_diff(root, base, path, deadline)
        old_path = previous_path or path
        for candidate in (path, old_path):
            if candidate.startswith("/") or any(
                part in {"", ".."} for part in candidate.split("/")
            ):
                raise ValueError("Invalid repository-relative path")
        repo = _reference_repo(reference)
        token = _token() if repo is not None else None
        if repo is None or token is None:
            raise ValueError(_PR_LOAD_FAILED)
        with self._client(repo.org, token, deadline=deadline) as client:
            try:
                pr = _call(client.get_pull_request, repo.project, repo.repo, reference.number)
            except _RestFailure as exc:
                raise ValueError(_PR_LOAD_FAILED) from exc
            current_head = _commit(pr, "lastMergeSourceCommit")
            current_base = _commit(pr, "lastMergeTargetCommit")
            if current_head is None or current_base is None:
                raise ValueError(_UNEXPECTED_RESPONSE)
            if (head_sha and head_sha != current_head) or (base_sha and base_sha != current_base):
                raise ValueError("The pull request changed; refresh before expanding context")
            if _has_commits(root, current_head, current_base):
                merge_base = _merge_base(root, current_base, current_head)
                if merge_base is not None:
                    return {
                        "object": FILE_DIFF_OBJECT,
                        "path": path,
                        "before": _pr_text(root, merge_base, old_path),
                        "after": _pr_text(root, current_head, path),
                    }
            try:
                iterations = _call(client.iterations, repo.project, repo.repo, reference.number)
            except _RestFailure as exc:
                raise ValueError(_PR_LOAD_FAILED) from exc
            # Each iteration records the merge base it was compared with.
            merge_base = _commit(_latest_iteration(iterations), "commonRefCommit")
            if merge_base is None:
                raise ValueError(_UNEXPECTED_RESPONSE)

            def contents(commit: str, filename: str) -> str | None:
                try:
                    text = _call(
                        client.item_content, repo.project, repo.repo, f"/{filename}", commit
                    )
                except _RestFailure as exc:
                    raise ValueError(_REVISION_LOAD_FAILED) from exc
                if text is not None and "\x00" in text:
                    raise ValueError(_NO_CONTEXT)
                return text

            return {
                "object": FILE_DIFF_OBJECT,
                "path": path,
                "before": contents(merge_base, old_path),
                "after": contents(current_head, path),
            }

    def _checkout_file_diff(
        self, root: str, base: str, path: str, deadline: float
    ) -> dict[str, Any]:
        """Diff one file of the local checkout: HEAD against its merge base with ``base``.

        Invalid UTF-8 is decoded leniently, as the GitHub panel does.
        """
        resolved = base or self._branch_base(root, deadline)
        run = _text_run(root)
        diff_base = local_git.resolve_diff_base(run, resolved) if resolved else None
        return {
            "object": FILE_DIFF_OBJECT,
            "path": path,
            "before": None if diff_base is None else local_git.read_file(run, diff_base, path),
            "after": local_git.read_file(run, "HEAD", path),
        }

    def _branch_base(self, root: str, deadline: float) -> str | None:
        """Return the target branch of the checked-out branch's PR, or ``None``."""
        repo = _target_repo(root, None)
        token = _token() if repo is not None else None
        if repo is None or token is None:
            return None
        try:
            with self._client(repo.org, token, deadline=deadline) as client:
                listed = _branch_pr(client, repo, _branch(root))
        except _RestFailure:
            return None
        return None if listed is None else _short_ref(listed.get("targetRefName"))

    def set_preference(
        self,
        root: str,  # noqa: ARG002 - there is no preference to save
        reference: PullRequestRef | None,  # noqa: ARG002 - there is no preference to save
        *,
        account: str | None,
        remote: str | None,
    ) -> None:
        """Reject every choice: Azure DevOps has no account or base remote selection.

        :raises ValueError: When ``account`` or ``remote`` is given.
        """
        if account is not None or remote is not None:
            raise ValueError("Azure DevOps pull requests have no account or base remote choice")

    def shell_pr_operations(self, segments: Sequence[ShellSegment]) -> list[ShellPrOp]:
        """Return one op per ``az repos pr`` command, in order."""
        return azure_devops_observer.shell_pr_operations(segments)

    def pr_from_object(self, obj: Mapping[str, object]) -> PullRequestRef | None:
        """Return the PR that ``pullRequestId`` and the ``repository`` field name."""
        return azure_devops_observer.pr_from_object(obj)

    def mcp_prs(
        self, tool_name: str, arguments: dict[str, object], result: object
    ) -> tuple[list[PullRequestRef], bool] | None:
        """Return ``None``: no Azure DevOps MCP tool is tracked."""
        return azure_devops_observer.mcp_prs(tool_name, arguments, result)


PULL_REQUESTS: PullRequestFacet = AzureDevOpsPullRequests()
