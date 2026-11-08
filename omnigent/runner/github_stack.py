"""Find a GitHub pull request's parent in a stack and read the changes made since it.

A same-repository stack bases each pull request on its parent's branch, so GitHub's
own diff already shows one pull request's changes. Fork stacks, such as those that
Databricks ``git stack`` creates, base every pull request on the default branch. There
the parent's head is the newest of this pull request's commits that is another pull
request's head, and the per-PR diff runs from that commit to this head.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

from omnigent.runner import github_resource as github
from omnigent.runner.git_providers import PR_STACK_DIFF_OBJECT, PR_STACK_OBJECT
from omnigent.runner.session_prs import PullRequestRef

# Newest commits checked for another pull request's head, all in one GraphQL request.
MAX_STACK_COMMITS = 100
# A parent's head commit also belongs to every pull request stacked above it.
_MAX_ASSOCIATED_PRS = 25
# GitHub's comparison lists at most this many files.
MAX_COMPARE_FILES = 300
_PARENT_STATES = frozenset({"OPEN", "MERGED"})
# Databricks ``git stack`` writes this section with a ``files/<parent>..<head>`` link.
_STACK_SECTION = re.compile(
    r"^##[ \t]*\N{PANCAKES}[ \t]*Stacked PR\b(?P<section>.*?)(?=^#{1,2}[ \t]|\Z)",
    re.MULTILINE | re.DOTALL,
)
_STACK_RANGE = re.compile(
    r"/files/(?P<parent>[0-9a-f]{40}(?:[0-9a-f]{24})?)\.\.[0-9a-f]{40}(?:[0-9a-f]{24})?\b"
)
_LOOKUP_FAILED = "GitHub could not look up the pull request's stack. Refresh to retry."
_DIFF_FAILED = "GitHub could not load the changes since the parent pull request. Refresh to retry."


def _text(value: object, *keys: str) -> str | None:
    """Read an optional nonempty string from nested GitHub JSON."""
    for key in keys:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, str) and value else None


def _parent(
    *,
    number: object,
    url: object,
    title: object,
    state: object,
    head_sha: str,
    source: str,
) -> dict[str, Any]:
    return {
        "number": number if isinstance(number, int) else None,
        "url": url if isinstance(url, str) else None,
        "title": title if isinstance(title, str) else None,
        "state": state.upper() if isinstance(state, str) else None,
        "head_sha": head_sha,
        "source": source,
    }


def _gh_json(root: str, reference: PullRequestRef, argv: list[str]) -> Any:
    rc, out, _ = github._gh(
        ["api", *github._host_args(root, reference), *argv],
        cwd=root,
        token=github._pr_token(root, reference),
    )
    if rc != 0:
        raise ValueError(_LOOKUP_FAILED)
    try:
        return json.loads(out)
    except ValueError:
        raise ValueError(_LOOKUP_FAILED) from None


def _base_branch_parent(
    root: str, reference: PullRequestRef, base_ref: str
) -> dict[str, Any] | None:
    """Return the open same-repository pull request whose head branch is ``base_ref``."""
    owner = reference.repository.split("/", 1)[0]
    pulls = _gh_json(
        root,
        reference,
        [
            f"repos/{reference.repository}/pulls"
            f"?state=open&per_page=5&head={quote(owner)}:{quote(base_ref, safe='')}"
        ],
    )
    if not isinstance(pulls, list):
        raise ValueError(_LOOKUP_FAILED)
    for pull in pulls:
        head_sha = _text(pull, "head", "sha")
        same_repository = (_text(pull, "head", "repo", "full_name") or "").lower()
        if (
            head_sha
            and _text(pull, "head", "ref") == base_ref
            and same_repository == reference.repository.lower()
        ):
            return _parent(
                number=pull.get("number"),
                url=pull.get("html_url"),
                title=pull.get("title"),
                state=pull.get("state"),
                head_sha=head_sha,
                source="base_branch",
            )
    return None


def _heads_query(count: int) -> str:
    """One query reading the pull requests associated with each of ``count`` commits."""
    variables = ", ".join(f"$c{index}: GitObjectID!" for index in range(count))
    objects = " ".join(
        f"c{index}: object(oid: $c{index}) {{ ...Heads }}" for index in range(count)
    )
    return (
        f"query($owner: String!, $name: String!, {variables}) "
        f"{{ repository(owner: $owner, name: $name) {{ {objects} }} }} "
        "fragment Heads on Commit { "
        f"associatedPullRequests(first: {_MAX_ASSOCIATED_PRS}, "
        "orderBy: {field: CREATED_AT, direction: ASC}) { "
        "nodes { number url title state headRefOid baseRepository { nameWithOwner } } } }"
    )


def _associated_pull_requests(
    root: str, reference: PullRequestRef, repository: str, shas: Sequence[str]
) -> dict[str, list[dict[str, Any]]]:
    """Map each commit to its associated pull requests, read from ``repository``.

    GitHub associates an open fork pull request with its commits only in the fork, so
    ``repository`` is the head repository. A commit the repository lacks maps to nothing.
    """
    owner, name = repository.split("/", 1)
    variables: dict[str, str] = {"owner": owner, "name": name}
    variables.update({f"c{index}": sha for index, sha in enumerate(shas)})
    host = github._host_args(root, reference)
    token = github._pr_token(root, reference)
    if github._gh_timed_out.get():
        raise ValueError(_LOOKUP_FAILED)
    # gh exits nonzero when one alias errors, yet the other aliases' data stays usable.
    _, out, _ = github._pr_json_write(
        root,
        host=host,
        token=token,
        method="POST",
        endpoint="graphql",
        fields={"query": _heads_query(len(shas)), "variables": variables},
    )
    try:
        response = json.loads(out)
    except ValueError:
        raise ValueError(_LOOKUP_FAILED) from None
    data = response.get("data") if isinstance(response, dict) else None
    found = data.get("repository") if isinstance(data, dict) else None
    if not isinstance(found, dict):
        raise ValueError(_LOOKUP_FAILED)
    associated: dict[str, list[dict[str, Any]]] = {}
    for index, sha in enumerate(shas):
        commit = found.get(f"c{index}")
        connection = commit.get("associatedPullRequests") if isinstance(commit, dict) else None
        nodes = connection.get("nodes") if isinstance(connection, dict) else None
        associated[sha] = [node for node in nodes or [] if isinstance(node, dict)]
    return associated


def _commit_parent(
    root: str,
    reference: PullRequestRef,
    *,
    head_repository: str,
    commits: Sequence[str],
    head_sha: str,
) -> dict[str, Any] | None:
    """Return the pull request whose head is this PR's newest commit that heads another PR."""
    candidates = [sha for sha in reversed(commits) if sha != head_sha][:MAX_STACK_COMMITS]
    if not candidates:
        return None
    associated = _associated_pull_requests(root, reference, head_repository, candidates)
    for sha in candidates:
        for pull in associated.get(sha, []):
            base_repository = _text(pull, "baseRepository", "nameWithOwner") or ""
            if (
                pull.get("headRefOid") == sha
                and pull.get("number") != reference.number
                and pull.get("state") in _PARENT_STATES
                and base_repository.lower() == reference.repository.lower()
            ):
                return _parent(
                    number=pull.get("number"),
                    url=pull.get("url"),
                    title=pull.get("title"),
                    state=pull.get("state"),
                    head_sha=sha,
                    source="commit",
                )
    return None


def description_parent_sha(body: object) -> str | None:
    """Return the parent head from a ``🥞 Stacked PR`` section's ``files/<a>..<b>`` link."""
    if not isinstance(body, str):
        return None
    section = _STACK_SECTION.search(body)
    found = _STACK_RANGE.search(section.group("section")) if section else None
    return found.group("parent") if found else None


def pr_stack(root: str, reference: PullRequestRef, *, head_sha: str) -> dict[str, Any]:
    """Find the PR's parent in its stack, at ``head_sha``.

    A PR based on another open PR's branch is already a per-PR diff, so its parent has
    ``source: "base_branch"``. A PR on the default branch takes its parent from its own
    commits (``"commit"``), then from its description's stack link (``"description"``).
    A PR based on any other branch has no parent.

    :raises ValueError: When the PR moved from ``head_sha`` or GitHub cannot answer.
    """
    with github._gh_deadline_scope(time.monotonic() + github._PR_EDIT_REQUEST_SECONDS):
        pr = github._check_pr_revision(root, reference, head_sha=head_sha)
        base_ref = _text(pr, "base", "ref")
        default_branch = _text(pr, "base", "repo", "default_branch")
        parent: dict[str, Any] | None = None
        if base_ref and default_branch and base_ref != default_branch:
            parent = _base_branch_parent(root, reference, base_ref)
        else:
            commits = [entry["sha"] for entry in github._pr_commit_entries(root, reference)]
            parent = _commit_parent(
                root,
                reference,
                head_repository=_text(pr, "head", "repo", "full_name") or reference.repository,
                commits=commits,
                head_sha=head_sha,
            )
            described = description_parent_sha(pr.get("body"))
            if parent is None and described and described != head_sha and described in commits:
                parent = _parent(
                    number=None,
                    url=None,
                    title=None,
                    state=None,
                    head_sha=described,
                    source="description",
                )
    return {"object": PR_STACK_OBJECT, "head_sha": head_sha, "parent": parent}


def pr_stack_diff(
    root: str, reference: PullRequestRef, *, head_sha: str, parent_sha: str
) -> dict[str, Any]:
    """Return the changed files and patch from ``parent_sha`` to the PR's head.

    ``parent_sha`` must be one of the PR's own commits, so only the PR's history is
    served. Both ends are fixed commits, so the result never changes once read.

    :raises ValueError: When the PR moved from ``head_sha``, ``parent_sha`` is not one of
        its commits, or GitHub cannot compare them.
    """
    if parent_sha == head_sha:
        raise ValueError("The parent commit must differ from the pull request's head.")
    host = github._host_args(root, reference)
    token = github._pr_token(root, reference)
    endpoint = f"repos/{reference.repository}/compare/{parent_sha}...{head_sha}"
    with github._gh_deadline_scope(time.monotonic() + github._PR_EDIT_REQUEST_SECONDS):
        github._check_pr_revision(root, reference, head_sha=head_sha)
        if parent_sha not in {
            entry["sha"] for entry in github._pr_commit_entries(root, reference)
        }:
            raise ValueError("That commit is not part of the selected pull request.")
        rc, out, _ = github._gh(["api", *host, endpoint], cwd=root, token=token)
        if rc != 0:
            raise ValueError(_DIFF_FAILED)
        try:
            comparison = json.loads(out)
        except ValueError:
            raise ValueError(_DIFF_FAILED) from None
        entries = comparison.get("files") if isinstance(comparison, dict) else None
        if not isinstance(entries, list):
            raise ValueError(_DIFF_FAILED)
        rc, patch, _ = github._gh(
            ["api", *host, "-H", "Accept: application/vnd.github.diff", endpoint],
            cwd=root,
            token=token,
        )
        if rc != 0:
            raise ValueError(_DIFF_FAILED)
    files = github._changed_file_payloads(entries)
    result: dict[str, Any] = {
        "object": PR_STACK_DIFF_OBJECT,
        "patch": patch,
        "files": files,
        "has_more": len(entries) >= MAX_COMPARE_FILES,
        "head_sha": head_sha,
        "parent_sha": parent_sha,
    }
    if result["has_more"]:
        result["warning"] = (
            f"The provider lists at most {MAX_COMPARE_FILES} files; some files are not shown."
        )
    return result
