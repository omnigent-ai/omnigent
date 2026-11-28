"""Searching GitHub: a session pull request's reviewers and completions, and a branch's PRs.

Each search names a host the user set up for GitHub before ``gh`` runs, so a token in
the environment never goes to a host taken from a remote or a request. The caller's
text never reaches GitHub as search syntax: it goes in as a GraphQL variable, or as
quoted words. What GitHub answers is untrusted: a pull request URL outside the
repository is dropped, and a client shows titles as plain text.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from omnigent.git_providers import EnvInstances, host_of
from omnigent.git_providers.github import GitHubProvider
from omnigent.runner import github_resource as github
from omnigent.runner.pull_request_errors import InvalidPullRequestInput, PullRequestError
from omnigent.runner.session_prs import PullRequestRef

MAX_REVIEWERS = 20
MAX_COMPLETIONS = 10
_SEARCH_OPERATORS = frozenset({"AND", "OR", "NOT"})
_UNKNOWN_HOST = "Sign in to this GitHub host with gh before searching it"
_UNREADABLE = "GitHub couldn't answer this search. Try again."
MAX_BRANCH_PULL_REQUESTS = 20
# GitHub's owner and repository names; anything else would change the REST path.
_REPOSITORY_NAME = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_UNKNOWN_WORKSPACE_HOST = "The workspace's repository isn't on a GitHub host set up here."
_UNLISTED = "GitHub couldn't list this branch's pull requests. Try again."


@contextmanager
def _searching(root: str, reference: PullRequestRef) -> Iterator[str | None]:
    """One deadline for a search's calls, and the token of its repository's account.

    The host is checked first, so no ``gh`` call, the token lookup included, names a host
    the user never set up for GitHub. With no account chosen, or its token unreadable, the
    search runs as gh's active account, as the reviewer picker's read does.
    """
    if not github._known_github_host(reference.host):
        raise PullRequestError(_UNKNOWN_HOST)
    with github._gh_deadline_scope(time.monotonic() + github._PR_EDIT_REQUEST_SECONDS):
        yield github._pr_token(root, reference)


def _query(
    root: str, reference: PullRequestRef, query: str, token: str | None, **variables: Any
) -> dict[str, Any]:
    """One read-only GraphQL query on the pull request's host, keeping partial data."""
    _, output, _ = github._pr_json_write(
        root,
        host=["--hostname", reference.host],
        token=token,
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


def quoted_words(text: str) -> str:
    """``text`` as quoted search words, refusing GitHub search syntax.

    :raises InvalidPullRequestInput: For a qualifier (``is:open``), an exclusion (``-x``),
        a quote, or a boolean operator.
    """
    words = text.split()
    for word in words:
        if ":" in word or '"' in word or word.startswith("-") or word in _SEARCH_OPERATORS:
            raise InvalidPullRequestInput("Search for words only, without search qualifiers")
    return " ".join(f'"{word}"' for word in words)


def _candidates(nodes: Any, key: str, kind: str) -> list[dict[str, Any]]:
    """Reviewer candidates in the shape the reviewer picker lists them."""
    found = []
    for node in nodes if isinstance(nodes, list) else []:
        login = node.get(key) if isinstance(node, dict) else None
        if isinstance(login, str) and login:
            display = node.get("name")
            found.append(
                {
                    "login": login,
                    "kind": kind,
                    "name": display if isinstance(display, str) and display else None,
                }
            )
    return found


_PEOPLE_QUERY = """query($owner: String!, $name: String!, $q: String!, $first: Int!) {
  repository(owner: $owner, name: $name) {
    assignableUsers(query: $q, first: $first) { nodes { login name } }
  }
}"""


def _people(
    root: str, reference: PullRequestRef, token: str | None, text: str, first: int
) -> list[dict[str, Any]]:
    """The repository's assignable people matching ``text``, in GitHub's best-match order.

    :raises PullRequestError: When the repository or its people can't be read, so that
        isn't shown as a search with no matches.
    """
    owner, name = reference.repository.split("/", 1)
    data = _query(
        root, reference, _PEOPLE_QUERY, token, owner=owner, name=name, q=text, first=first
    )
    repository = data.get("repository")
    people = repository.get("assignableUsers") if isinstance(repository, dict) else None
    nodes = people.get("nodes") if isinstance(people, dict) else None
    if not isinstance(nodes, list):
        raise PullRequestError(_UNREADABLE)
    return _candidates(nodes, "login", "user")


def _matching_teams(
    root: str, reference: PullRequestRef, token: str | None, text: str
) -> tuple[list[dict[str, Any]], bool]:
    """The teams with access to the repository whose slug or name contains ``text``.

    The reviewer picker's own list, so a team GitHub would refuse as a reviewer is never
    offered. A personal repository has none, and a list gh can't read leaves them out.
    The flag says the list stopped at its cap, so a match may be missing.
    """
    teams, truncated = github._repo_teams(root, ["--hostname", reference.host], token, reference)
    needle = text.strip().casefold()
    matching = [
        team
        for team in teams
        if needle in team["login"].casefold() or needle in (team["name"] or "").casefold()
    ]
    return matching, truncated


def search_reviewers(
    root: str, reference: PullRequestRef, *, text: str, page_size: int
) -> dict[str, Any]:
    """Up to ``page_size`` people and teams who can review ``reference``, matching ``text``.

    People come first, in GitHub's best-match order, then the teams with access to the
    repository, in the order GitHub lists them. ``has_more`` says more would match.

    :param page_size: At most :data:`MAX_REVIEWERS` in all; 0 asks for that many.
    """
    if not 0 <= page_size <= MAX_REVIEWERS:
        raise InvalidPullRequestInput(f"Ask for at most {MAX_REVIEWERS} reviewers")
    first = page_size or MAX_REVIEWERS
    with _searching(root, reference) as token:
        # One past the page tells a full page of people from a cut one.
        users = _people(root, reference, token, text, first + 1)
        teams, truncated = _matching_teams(root, reference, token, text)
    found = users + teams
    return {"object": "list", "data": found[:first], "has_more": truncated or len(found) > first}


_REFERENCES_QUERY = """query($q: String!, $first: Int!) {
  search(type: ISSUE, query: $q, first: $first) {
    nodes { ... on PullRequest { url number title state isDraft } }
  }
}"""


def _in_repository(url: Any, reference: PullRequestRef) -> bool:
    if not isinstance(url, str):
        return False
    try:
        found = PullRequestRef.from_url(url)
    except ValueError:
        return False
    return (
        found.provider == "github"
        and found.host == reference.host
        and found.repository.lower() == reference.repository.lower()
    )


def _references(root: str, reference: PullRequestRef, text: str) -> list[dict[str, Any]]:
    """The repository's pull requests matching ``text``, newest-matching first."""
    query = f"is:pr repo:{reference.repository} {quoted_words(text)}".strip()
    with _searching(root, reference) as token:
        data = _query(root, reference, _REFERENCES_QUERY, token, q=query, first=MAX_COMPLETIONS)
    search = data.get("search")
    nodes = search.get("nodes") if isinstance(search, dict) else None
    if not isinstance(nodes, list):
        raise PullRequestError(_UNREADABLE)
    return [
        {
            "url": node["url"],
            "number": node.get("number"),
            "title": node.get("title") if isinstance(node.get("title"), str) else "",
            "state": node.get("state"),
            "is_draft": node.get("isDraft") is True,
        }
        for node in nodes
        if isinstance(node, dict) and _in_repository(node.get("url"), reference)
    ]


def search_completions(
    root: str, reference: PullRequestRef, *, kind: str, text: str
) -> dict[str, Any]:
    """An editor's completions in ``reference``'s repository.

    :param kind: ``"MENTION"`` for the people who can be mentioned after ``@``, or
        ``"REFERENCE"`` for the repository's pull requests after ``#``.
    """
    if kind == "MENTION":
        with _searching(root, reference) as token:
            people = _people(root, reference, token, text, MAX_COMPLETIONS)
        return {"object": "list", "kind": "mention", "people": people}
    if kind != "REFERENCE":
        raise InvalidPullRequestInput("Choose what to complete")
    return {
        "object": "list",
        "kind": "reference",
        "pull_requests": _references(root, reference, text),
    }


def _config_value(root: str, key: str) -> str | None:
    rc, output, _ = github._git(["config", "--get", key], cwd=root)
    value = output.strip()
    return value if rc == 0 and value and value != "." else None


def _remote_repository(root: str, remote: str) -> tuple[str, str] | None:
    """The host and ``owner/repo`` a remote names, by the provider's port-aware parser.

    ``None`` for a missing remote, a path, a host never set up for GitHub, or a name
    GitHub wouldn't give a repository.
    """
    rc, url, _ = github._git(["remote", "get-url", remote], cwd=root)
    if rc != 0:
        return None
    parsed = GitHubProvider().parse_remote_url(url.strip(), EnvInstances())
    if parsed is None or not _REPOSITORY_NAME.fullmatch(parsed.repository):
        return None
    return parsed.host, parsed.repository


def _base_remote(root: str) -> str:
    """The remote pull requests are opened against, as gh resolves it.

    The one ``gh repo set-default`` chose, else the checked-out branch's tracking remote
    when it names a GitHub repository, else ``origin``.
    """
    rc, output, _ = github._git(["config", "--get-regexp", r"^remote\..*\.gh-resolved$"], cwd=root)
    for line in output.splitlines() if rc == 0 else []:
        key, _, value = line.partition(" ")
        if value.strip() == "base":
            return key.removeprefix("remote.").removesuffix(".gh-resolved")
    rc, checked_out, _ = github._git(["symbolic-ref", "--quiet", "--short", "HEAD"], cwd=root)
    tracking = _config_value(root, f"branch.{checked_out.strip()}.remote") if rc == 0 else None
    if tracking and _remote_repository(root, tracking) is not None:
        return tracking
    return "origin"


def _push_remote(root: str, branch: str) -> str:
    """The remote ``branch`` is pushed to, by git's own order of settings."""
    return (
        _config_value(root, f"branch.{branch}.pushRemote")
        or _config_value(root, "remote.pushDefault")
        or _config_value(root, f"branch.{branch}.remote")
        or "origin"
    )


def workspace_repository(root: str) -> tuple[str, str] | None:
    """The host and ``owner/repo`` of the workspace's base remote, read from git alone.

    :returns: e.g. ``("github.com", "example-org/example-repo")``, or ``None`` when the
        remote is missing or doesn't name a repository on a host set up for GitHub.
    :raises PullRequestError: For a remote on a host never set up for GitHub, so that
        isn't reported as a branch with no pull requests.
    """
    remote = _base_remote(root)
    found = _remote_repository(root, remote)
    if found is not None:
        return found
    rc, url, _ = github._git(["remote", "get-url", remote], cwd=root)
    host = host_of(url.strip()) if rc == 0 else None
    if host and not github._known_github_host(host):
        raise PullRequestError(_UNKNOWN_WORKSPACE_HOST)
    return None


def _head(entry: dict[str, Any]) -> tuple[Any, Any]:
    """The branch a pull request's head is, and ``owner/repo`` of the repository it's in.

    The repository is ``None`` once it's deleted.
    """
    head = entry.get("head")
    head = head if isinstance(head, dict) else {}
    repository = head.get("repo")
    return head.get("ref"), repository.get("full_name") if isinstance(repository, dict) else None


def _state(entry: dict[str, Any]) -> str | None:
    """``OPEN``, ``CLOSED`` or ``MERGED``, as GitHub's GraphQL API names a pull request's."""
    if entry.get("merged_at"):
        return "MERGED"
    state = entry.get("state")
    return state.upper() if isinstance(state, str) else None


def _repository_token(root: str, host: str, repository: str) -> str | None:
    """The token of the account chosen for ``repository``, or ``None`` for gh's active one."""
    if github._in_sandbox():
        return None
    # Saved under the repository as a pull request URL names it, which is lower-cased.
    login = github._config.github_account_preference(f"{host}/{repository.lower()}")
    return github._gh_auth_token(root, login, host) if login else None


def _same_repository(url: str, host: str, repository: str) -> bool:
    try:
        reference = PullRequestRef.from_url(url)
    except ValueError:
        return False
    return (
        reference.provider == "github"
        and reference.host == host
        and reference.repository.lower() == repository.lower()
    )


def branch_pull_requests(root: str, *, branch: str) -> dict[str, Any]:
    """The pull requests from the workspace's ``branch``, in the workspace's own repository.

    A pull request counts only when its head is ``branch`` in the repository the workspace
    pushes it to, so a same-named branch in someone else's fork isn't this branch. The most
    recently opened :data:`MAX_BRANCH_PULL_REQUESTS` in any state, newest first; ``has_more``
    says older ones exist. A workspace without a repository remote has none.

    :param branch: A branch name, checked by the caller against git's ref rules.
    :raises PullRequestError: When the remote's host isn't set up for GitHub, which is
        refused before any ``gh`` call, or when GitHub's answer can't be read.
    """
    found = workspace_repository(root)
    if found is None:
        return {"object": "list", "data": [], "has_more": False}
    host, repository = found
    # A fork workflow pushes to its own remote and opens pull requests on the base.
    pushed_to = _remote_repository(root, _push_remote(root, branch))
    source = pushed_to[1] if pushed_to is not None and pushed_to[0] == host else repository
    owner = source.split("/", 1)[0]
    with github._gh_deadline_scope(time.monotonic() + github._PR_EDIT_REQUEST_SECONDS):
        # GitHub keeps only the owner's branch before it cuts the page, so other forks'
        # same-named branches can't crowd this one out. Each field is sent URL-encoded.
        rc, output, _ = github._gh(
            [
                "api",
                "--hostname",
                host,
                "--method",
                "GET",
                f"repos/{repository}/pulls",
                "-f",
                f"head={owner}:{branch}",
                "-f",
                "state=all",
                "-F",
                # One past the cap tells a full list from a cut one.
                f"per_page={MAX_BRANCH_PULL_REQUESTS + 1}",
            ],
            cwd=root,
            token=_repository_token(root, host, repository),
        )
    if rc != 0:
        raise PullRequestError(_UNLISTED)
    try:
        entries = json.loads(output)
    except ValueError as exc:
        raise PullRequestError(_UNLISTED) from exc
    if not isinstance(entries, list):
        entries = []
    rows = [
        {
            "url": entry["html_url"],
            "number": entry.get("number"),
            "title": entry.get("title") if isinstance(entry.get("title"), str) else "",
            "state": _state(entry),
            "is_draft": entry.get("draft") is True,
            "updated_at": entry.get("updated_at"),
            "head_ref": branch,
        }
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("html_url"), str)
        # What GitHub answers is untrusted, so the head filter is checked again here.
        and _head(entry)[0] == branch
        and str(_head(entry)[1] or "").lower() == source.lower()
        and _same_repository(entry["html_url"], host, repository)
    ]
    return {
        "object": "list",
        "data": rows[:MAX_BRANCH_PULL_REQUESTS],
        "has_more": len(rows) > MAX_BRANCH_PULL_REQUESTS,
    }
