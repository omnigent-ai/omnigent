"""GraphQL reads and mutations for pull request writes, with the head guard they share."""

from __future__ import annotations

import json
import time
from typing import Any

from omnigent.runner import github_failures
from omnigent.runner import github_resource as github
from omnigent.runner.github_review_threads import _graphql
from omnigent.runner.pull_request_errors import PullRequestChangedError, UnknownOutcomeError
from omnigent.runner.session_prs import PullRequestRef

_LOAD_FAILED = "GitHub could not load this pull request. Refresh to retry."
AUTHENTICATION_TIMED_OUT = "GitHub authentication timed out. Nothing was sent; try again."


def write_token(root: str, reference: PullRequestRef) -> str | None:
    """The token a write runs with, or a refusal before it's sent as another account.

    Call it inside the write's deadline scope. Without the token of the login chosen for the
    repository, ``gh`` would run as whichever account is active on the host.
    """
    token = github._pr_token(root, reference)
    if github._gh_timed_out.get():
        raise ValueError(AUTHENTICATION_TIMED_OUT)
    if token is None and not github._in_sandbox():
        login = github._config.github_account_preference(github._repo_argument(reference))
        if login:
            raise ValueError(
                f"The selected GitHub account {login} isn't signed in on the host. "
                "Sign in with gh or choose another account."
            )
    return token


def pull_request_node(root: str, reference: PullRequestRef, selection: str) -> dict[str, Any]:
    """Read the pull request's ``selection`` fields, e.g. ``"id headRefOid"``."""
    owner, name = reference.repository.split("/", 1)
    query = (
        "query($owner: String!, $name: String!, $number: Int!) {"
        " repository(owner: $owner, name: $name) {"
        f" pullRequest(number: $number) {{ {selection} }} }} }}"
    )
    with github._gh_deadline_scope(time.monotonic() + github._PR_EDIT_REQUEST_SECONDS):
        response = _graphql(
            root, reference, query, owner=owner, name=name, number=reference.number
        )
    node = ((response.get("data") or {}).get("repository") or {}).get("pullRequest")
    if not isinstance(node, dict):
        raise ValueError(_LOAD_FAILED)
    return node


def require_head(root: str, reference: PullRequestRef, expected: str) -> dict[str, Any]:
    """Read the pull request and refuse, before any write, when its head isn't ``expected``."""
    node = pull_request_node(root, reference, "id headRefOid state isDraft")
    if node.get("headRefOid") != expected:
        raise PullRequestChangedError()
    return node


def refusal_or_moved_head(
    root: str, reference: PullRequestRef, expected_head: str | None, refusal: ValueError
) -> ValueError:
    """The moved-head error if the head is no longer ``expected_head``, else ``refusal``.

    The head is read again rather than matched by GitHub's message text. A head that
    can't be read keeps the original refusal; an unknown outcome stays unknown.
    """
    if expected_head is None or isinstance(refusal, UnknownOutcomeError):
        return refusal
    try:
        current = pull_request_node(root, reference, "headRefOid").get("headRefOid")
    except ValueError:
        return refusal
    return refusal if current == expected_head else PullRequestChangedError()


def mutate(
    root: str,
    reference: PullRequestRef,
    query: str,
    payload_name: str,
    *,
    action: str,
    expected_head_oid: str | None = None,
    **variables: Any,
) -> dict[str, Any]:
    """Run one GraphQL mutation and return its named payload.

    A clean answer with the payload is success. GraphQL errors in an answer that parsed and
    has no payload, or a definite HTTP refusal, are a refusal, or a moved head when
    ``expected_head_oid`` no longer heads the pull request. Errors beside a payload, a
    timeout, a 5xx, a reset connection or an unreadable answer are an unknown outcome: the
    write is never retried.
    """
    with github._gh_deadline_scope(time.monotonic() + github._PR_EDIT_REQUEST_SECONDS):
        host = github._host_args(root, reference)
        token = write_token(root, reference)
        rc, output, err = github._pr_json_write(
            root,
            host=host,
            token=token,
            method="POST",
            endpoint="graphql",
            fields={"query": query, "variables": variables},
        )
        if github._gh_timed_out.get() or rc is None:
            if rc is None and not github._gh_installed():
                raise github_failures.not_installed(reference.host)
            raise github_failures.unknown_outcome()
    try:
        response = json.loads(output)
    except ValueError:
        response = None
    payload = (
        (response.get("data") or {}).get(payload_name) if isinstance(response, dict) else None
    )
    if isinstance(response, dict) and response.get("errors"):
        if payload is not None:
            # The mutation ran; an error in a field it selected leaves its answer unreliable.
            raise github_failures.unknown_outcome()
        refused = github_failures.graphql_refusal(
            response["errors"], host=reference.host, action=action
        )
        raise refusal_or_moved_head(root, reference, expected_head_oid, refused)
    if rc == 0 and isinstance(payload, dict):
        return payload
    if rc != 0 and err:
        failure = github_failures.write_failure(err, host=reference.host, action=action)
        raise refusal_or_moved_head(root, reference, expected_head_oid, failure)
    raise github_failures.unknown_outcome()
