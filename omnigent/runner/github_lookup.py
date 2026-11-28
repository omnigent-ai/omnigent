"""Reading pull requests by URL, beyond the ones a session links, for its owner's lists.

Each read runs as gh's active account on the URL's host, never the account saved for a
repository, since a URL may name any repository. The host must be one the user set up
for GitHub, checked without running ``gh``: with an enterprise token in the environment,
``gh`` treats every host as signed in and would send that token to any host a URL names.

What GitHub answers is untrusted. Each answer keeps the URL that was asked about unless
GitHub's names the same pull request, and a client shows titles as plain text. A failure
says nothing GitHub or ``gh`` wrote, since that can name repositories.
"""

from __future__ import annotations

import json
import time
from typing import Any

from omnigent.runner import github_resource as github
from omnigent.runner.pull_request_errors import InvalidPullRequestInput, PullRequestError
from omnigent.runner.session_prs import PullRequestRef

MAX_PULL_REQUEST_URLS = 50
_CI_ROLLUPS = {
    "SUCCESS": "passing",
    "FAILURE": "failing",
    "ERROR": "failing",
    "PENDING": "pending",
    "EXPECTED": "pending",
}
_NOT_A_PULL_REQUEST = "Expected a GitHub pull request URL"
_UNKNOWN_HOST = "Sign in to this GitHub host with gh before reading its pull requests"
_UNREADABLE = "GitHub couldn't read these pull requests. Try again."
_NOT_FOUND = "GitHub has no such pull request, or this account can't see it."


def _reference(pr_url: str) -> PullRequestRef:
    """A GitHub pull request on a host the user set up, refused before any ``gh`` call."""
    try:
        reference = PullRequestRef.from_url(pr_url)
    except ValueError as exc:
        raise InvalidPullRequestInput(_NOT_A_PULL_REQUEST) from exc
    if reference.provider != "github":
        raise InvalidPullRequestInput(_NOT_A_PULL_REQUEST)
    if not github._known_github_host(reference.host):
        raise PullRequestError(_UNKNOWN_HOST)
    return reference


def _query(root: str, host: str, query: str, variables: dict[str, Any]) -> dict[str, Any]:
    """One read-only GraphQL query on ``host`` as gh's active account, keeping partial data."""
    with github._gh_deadline_scope(time.monotonic() + github._PR_EDIT_REQUEST_SECONDS):
        _, output, _ = github._pr_json_write(
            root,
            host=["--hostname", host],
            token=None,
            method="POST",
            endpoint="graphql",
            fields={"query": query, "variables": variables},
        )
    try:
        response = json.loads(output)
    except ValueError as exc:
        raise PullRequestError(_UNREADABLE) from exc
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict):
        raise PullRequestError(_UNREADABLE)
    return data


def _answered_url(reference: PullRequestRef, answered: Any) -> str:
    """GitHub's URL for the pull request when it names the same one, else the one asked about."""
    if not isinstance(answered, str):
        return reference.url
    try:
        found = PullRequestRef.from_url(answered)
    except ValueError:
        return reference.url
    same = (
        found.provider == reference.provider
        and found.host == reference.host
        and found.repository.lower() == reference.repository.lower()
        and found.number == reference.number
    )
    return found.url if same else reference.url


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


_STATS_FIELDS = """url additions deletions changedFiles updatedAt
  headRefOid baseRefOid state isDraft
  commits(last: 1) { nodes { commit { oid statusCheckRollup { state } } } }"""


def _stats_entry(reference: PullRequestRef, node: dict[str, Any]) -> dict[str, Any]:
    commits = node.get("commits")
    nodes = commits.get("nodes") if isinstance(commits, dict) else None
    last = nodes[-1] if isinstance(nodes, list) and nodes else None
    commit = last.get("commit") if isinstance(last, dict) else None
    commit = commit if isinstance(commit, dict) else {}
    rollup = commit.get("statusCheckRollup")
    return {
        "url": _answered_url(reference, node.get("url")),
        "repository": reference.repository,
        "number": reference.number,
        "additions": node.get("additions"),
        "deletions": node.get("deletions"),
        "changed_files": node.get("changedFiles"),
        "updated_at": node.get("updatedAt"),
        "head_sha": node.get("headRefOid"),
        "base_sha": node.get("baseRefOid"),
        "state": node.get("state"),
        "is_draft": node.get("isDraft") is True,
        "ci": _CI_ROLLUPS.get(str(rollup.get("state"))) if isinstance(rollup, dict) else None,
        "ci_head_sha": commit.get("oid"),
    }


def pull_request_list_stats(root: str, *, pr_urls: list[str]) -> dict[str, Any]:
    """Each named pull request's list-row stats, read in one query on one host.

    A pull request GitHub doesn't answer for, missing or not visible to the account, is
    left out. ``reader`` is the login the stats were read as.

    :param pr_urls: 1 to :data:`MAX_PULL_REQUEST_URLS` URLs, all on one host.
    :raises InvalidPullRequestInput: For no URLs, too many, one that isn't a GitHub pull
        request, or URLs on more than one host.
    :raises PullRequestError: For a host the user never set up, before any ``gh`` call,
        or when GitHub's answer can't be read.
    """
    if not 1 <= len(pr_urls) <= MAX_PULL_REQUEST_URLS:
        raise InvalidPullRequestInput(f"Ask for 1 to {MAX_PULL_REQUEST_URLS} pull requests")
    parsed: list[PullRequestRef] = []
    for url in pr_urls:
        try:
            parsed.append(PullRequestRef.from_url(url))
        except ValueError as exc:
            raise InvalidPullRequestInput(_NOT_A_PULL_REQUEST) from exc
    if len({reference.host for reference in parsed}) != 1:
        raise InvalidPullRequestInput("Ask for one code host's pull requests at a time")
    references = list({reference.url: _reference(reference.url) for reference in parsed}.values())
    declared: list[str] = []
    parts: list[str] = []
    variables: dict[str, Any] = {}
    for index, reference in enumerate(references):
        owner, name = reference.repository.split("/", 1)
        declared.append(f"$o{index}: String!, $n{index}: String!, $k{index}: Int!")
        parts.append(
            f"p{index}: repository(owner: $o{index}, name: $n{index}) "
            f"{{ pullRequest(number: $k{index}) {{ {_STATS_FIELDS} }} }}"
        )
        variables |= {f"o{index}": owner, f"n{index}": name, f"k{index}": reference.number}
    query = f"query({', '.join(declared)}) {{ viewer {{ login }} {' '.join(parts)} }}"
    data = _query(root, references[0].host, query, variables)
    entries = []
    for index, reference in enumerate(references):
        repository = data.get(f"p{index}")
        node = repository.get("pullRequest") if isinstance(repository, dict) else None
        if isinstance(node, dict):
            entries.append(_stats_entry(reference, node))
    viewer = data.get("viewer")
    login = viewer.get("login") if isinstance(viewer, dict) else None
    return {"object": "list", "reader": login if isinstance(login, str) else None, "data": entries}


_LOOKUP_QUERY = """query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) { pullRequest(number: $number) {
    url number title state isDraft updatedAt
  } }
}"""


def lookup_pull_request(root: str, *, pr_url: str) -> dict[str, Any]:
    """One pull request's title and state, by its URL.

    :raises InvalidPullRequestInput: For a URL that isn't a GitHub pull request.
    :raises PullRequestError: For a host the user never set up, before any ``gh`` call,
        or a pull request GitHub doesn't answer for.
    """
    reference = _reference(pr_url)
    owner, name = reference.repository.split("/", 1)
    data = _query(
        root,
        reference.host,
        _LOOKUP_QUERY,
        {"owner": owner, "name": name, "number": reference.number},
    )
    repository = data.get("repository")
    node = repository.get("pullRequest") if isinstance(repository, dict) else None
    if not isinstance(node, dict):
        raise PullRequestError(_NOT_FOUND)
    return {
        "object": "session.github.pull_request_lookup",
        "url": _answered_url(reference, node.get("url")),
        "repository": reference.repository,
        "number": reference.number,
        "title": _text(node.get("title")),
        "state": node.get("state"),
        "is_draft": node.get("isDraft") is True,
        "updated_at": node.get("updatedAt"),
    }
