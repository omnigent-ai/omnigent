"""Searching GitHub for the session's pull request: its reviewers, mentions and references.

The caller's text never reaches GitHub as search syntax: it goes in as a GraphQL
variable, or as quoted words. Each search names the pull request's host, which must be
one the user set up for GitHub, before ``gh`` runs. What GitHub answers is untrusted: a
pull request URL outside the repository is dropped, and a client shows titles as text.
"""

from __future__ import annotations

import json
import time
from typing import Any

from omnigent.runner import github_resource as github
from omnigent.runner.pull_request_errors import InvalidPullRequestInput, PullRequestError
from omnigent.runner.session_prs import PullRequestRef

MAX_REVIEWERS = 20
MAX_COMPLETIONS = 10
_SEARCH_OPERATORS = frozenset({"AND", "OR", "NOT"})
_UNKNOWN_HOST = "Sign in to this GitHub host with gh before searching it"
_UNREADABLE = "GitHub couldn't answer this search. Try again."


def _query(root: str, reference: PullRequestRef, query: str, **variables: Any) -> dict[str, Any]:
    """One read-only GraphQL query on the pull request's host, as its repository's account.

    GitHub's partial data is kept: a personal repository has no organization, so its team
    search fails while the people search answers.
    """
    if not github._known_github_host(reference.host):
        raise PullRequestError(_UNKNOWN_HOST)
    with github._gh_deadline_scope(time.monotonic() + github._PR_EDIT_REQUEST_SECONDS):
        _, output, _ = github._pr_json_write(
            root,
            host=["--hostname", reference.host],
            token=github._pr_token(root, reference),
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


def _connection_nodes(data: dict[str, Any], owner: str, field: str) -> Any:
    parent = data.get(owner)
    connection = parent.get(field) if isinstance(parent, dict) else None
    return connection.get("nodes") if isinstance(connection, dict) else None


def _matching_teams(
    root: str, reference: PullRequestRef, text: str, first: int
) -> list[dict[str, Any]]:
    """The teams with access to the repository whose slug or name contains ``text``.

    The reviewer picker's own list, so a team GitHub would refuse as a reviewer is never
    offered. A personal repository has none, and a list gh can't read leaves them out.
    """
    with github._gh_deadline_scope(time.monotonic() + github._PR_EDIT_REQUEST_SECONDS):
        teams, _more = github._repo_teams(
            root, ["--hostname", reference.host], github._pr_token(root, reference), reference
        )
    needle = text.strip().casefold()
    return [
        team
        for team in teams
        if needle in team["login"].casefold() or needle in (team["name"] or "").casefold()
    ][:first]


def search_reviewers(
    root: str, reference: PullRequestRef, *, text: str, page_size: int
) -> dict[str, Any]:
    """The people and teams who can review ``reference``, matching ``text``.

    People come first, in GitHub's best-match order, then the teams with access to the
    repository, in the order GitHub lists them.

    :param page_size: At most :data:`MAX_REVIEWERS` of each; 0 asks for that many.
    """
    if not 0 <= page_size <= MAX_REVIEWERS:
        raise InvalidPullRequestInput(f"Ask for at most {MAX_REVIEWERS} reviewers")
    owner, name = reference.repository.split("/", 1)
    first = page_size or MAX_REVIEWERS
    data = _query(root, reference, _PEOPLE_QUERY, owner=owner, name=name, q=text, first=first)
    users = _candidates(_connection_nodes(data, "repository", "assignableUsers"), "login", "user")
    teams = _matching_teams(root, reference, text, first)
    return {"object": "list", "data": users + teams}


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
    data = _query(root, reference, _REFERENCES_QUERY, q=query, first=MAX_COMPLETIONS)
    search = data.get("search")
    nodes = search.get("nodes") if isinstance(search, dict) else None
    if not isinstance(nodes, list):
        return []
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
        owner, name = reference.repository.split("/", 1)
        data = _query(
            root, reference, _PEOPLE_QUERY, owner=owner, name=name, q=text, first=MAX_COMPLETIONS
        )
        nodes = _connection_nodes(data, "repository", "assignableUsers")
        return {"object": "list", "kind": "mention", "people": _candidates(nodes, "login", "user")}
    if kind != "REFERENCE":
        raise InvalidPullRequestInput("Choose what to complete")
    return {
        "object": "list",
        "kind": "reference",
        "pull_requests": _references(root, reference, text),
    }
