"""GitHub pull request lifecycle changes: close and reopen, draft and ready, and auto-merge."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from omnigent.runner import github_failures
from omnigent.runner import github_resource as github
from omnigent.runner.github_graphql import AUTHENTICATION_TIMED_OUT, mutate, pull_request_node
from omnigent.runner.pull_request_errors import (
    InvalidPullRequestInput,
    PullRequestChangedError,
    UnknownOutcomeError,
)
from omnigent.runner.session_prs import PullRequestRef

MERGE_METHODS = frozenset({"merge", "squash", "rebase"})
_LOAD_FAILED = "GitHub could not load this pull request. Refresh to retry."
_AUTO_MERGE_STATE_SELECTION = "id headRefOid state autoMergeRequest { enabledAt }"
_AUTO_MERGE_MUTATION = """mutation($input: EnablePullRequestAutoMergeInput!) {
  enablePullRequestAutoMerge(input: $input) { pullRequest { id autoMergeRequest { enabledAt } } }
}"""


@dataclass(frozen=True)
class AutoMergeItem:
    """One pull request in an auto-merge batch, with the head the user saw."""

    reference: PullRequestRef
    merge_method: str
    expected_head_oid: str


def _run_pr_command(root: str, reference: PullRequestRef, args: list[str], *, action: str) -> None:
    """Run ``gh pr <args>`` on the pull request as its chosen account.

    :raises ValueError: A refusal only when GitHub definitely refused; any other failure
        after the send is an unknown outcome.
    """
    with github._gh_deadline_scope(time.monotonic() + github._PR_EDIT_REQUEST_SECONDS):
        github._host_args(root, reference)
        token = github._write_token(root, reference, timed_out=AUTHENTICATION_TIMED_OUT)
        rc, _, err = github._gh(
            ["pr", *args, str(reference.number), "-R", github._repo_argument(reference)],
            cwd=root,
            token=token,
        )
        if github._gh_timed_out.get() or rc is None:
            if rc is None and not github._gh_installed():
                raise github_failures.not_installed(reference.host)
            raise github_failures.unknown_outcome()
    if rc != 0:
        raise github_failures.write_failure(err, reference=reference, action=action)


def set_state(root: str, reference: PullRequestRef, *, state: str) -> dict[str, Any]:
    """Close (``closed``) or reopen (``open``) the pull request."""
    if state not in ("closed", "open"):
        raise InvalidPullRequestInput("Expected state to be closed or open")
    with github._one_pr_account(root, reference, timed_out=AUTHENTICATION_TIMED_OUT):
        return _set_state(root, reference, state=state)


def _set_state(root: str, reference: PullRequestRef, *, state: str) -> dict[str, Any]:
    current = github._pr_json(root, reference, "state")
    if current is None:
        raise ValueError(_LOAD_FAILED)
    if current.get("state") == "MERGED":
        raise ValueError("This pull request is merged, so its state can't change.")
    if state == "closed" and current.get("state") == "CLOSED":
        raise ValueError("This pull request is already closed.")
    if state == "open" and current.get("state") == "OPEN":
        raise ValueError("This pull request is already open.")
    if state == "closed":
        _run_pr_command(root, reference, ["close"], action="close this pull request")
    else:
        _run_pr_command(root, reference, ["reopen"], action="reopen this pull request")
    return {
        "object": "session.github.pr_state",
        "pr_url": reference.url,
        "state": "CLOSED" if state == "closed" else "OPEN",
    }


def set_draft(root: str, reference: PullRequestRef, *, draft: bool) -> dict[str, Any]:
    """Make the pull request a draft again (``draft``), or mark it ready for review."""
    with github._one_pr_account(root, reference, timed_out=AUTHENTICATION_TIMED_OUT):
        return _set_draft(root, reference, draft=draft)


def _set_draft(root: str, reference: PullRequestRef, *, draft: bool) -> dict[str, Any]:
    current = github._pr_json(root, reference, "state,isDraft")
    if current is None:
        raise ValueError(_LOAD_FAILED)
    if draft and current.get("state") == "MERGED":
        raise ValueError("This pull request is merged, so it can't become a draft.")
    if draft and current.get("isDraft") is True:
        raise ValueError("This pull request is already a draft.")
    if not draft and current.get("isDraft") is False:
        raise ValueError("This pull request is already ready for review.")
    if draft:
        _run_pr_command(
            root, reference, ["ready", "--undo"], action="convert this pull request to a draft"
        )
    else:
        _run_pr_command(root, reference, ["ready"], action="mark this pull request ready")
    return {"object": "session.github.pr_draft", "pr_url": reference.url, "is_draft": draft}


def enable_auto_merge(root: str, items: list[AutoMergeItem]) -> dict[str, Any]:
    """Turn on auto-merge in order, each pull request only while its head is the one given.

    The batch stops at the first pull request whose head moved. Its result is
    ``pull_request_changed``, and the pull requests after it are not sent, so they are
    absent from ``results``. Any other refusal is only that pull request's result, and
    one GitHub may have made but never confirmed is marked ``unconfirmed``.
    """
    if not items:
        raise InvalidPullRequestInput("Expected at least one pull request")
    for item in items:
        if item.merge_method not in MERGE_METHODS:
            raise InvalidPullRequestInput("Expected merge_method to be merge, squash, or rebase")
    results = []
    for item in items:
        result = _enable_one(root, item)
        results.append(result)
        if result.get("reason") == PullRequestChangedError.reason:
            break
    return {"object": "session.github.pr_auto_merge_batch", "results": results}


def _enable_one(root: str, item: AutoMergeItem) -> dict[str, Any]:
    """One pull request's auto-merge, with its read and its change run as one account."""
    result: dict[str, Any] = {"pr_url": item.reference.url}
    try:
        with github._one_pr_account(root, item.reference, timed_out=AUTHENTICATION_TIMED_OUT):
            result["enabled_at"] = _enable(root, item)
    except UnknownOutcomeError as exc:
        # Auto-merge may be on: a client must not offer this one as safe to retry.
        result.update(error=str(exc), reason=None, unconfirmed=True)
    except ValueError as exc:
        result.update(error=str(exc), reason=getattr(exc, "reason", None))
    return result


def _enable(root: str, item: AutoMergeItem) -> str:
    """Turn on auto-merge for one pull request, and answer when GitHub says it was enabled.

    :raises UnknownOutcomeError: When GitHub's answer doesn't confirm auto-merge on this
        pull request, since the change may still have been made.
    """
    node = pull_request_node(root, item.reference, _AUTO_MERGE_STATE_SELECTION)
    if node.get("headRefOid") != item.expected_head_oid:
        raise PullRequestChangedError()
    if node.get("state") != "OPEN":
        raise ValueError("Only an open pull request can merge automatically.")
    payload = mutate(
        root,
        item.reference,
        _AUTO_MERGE_MUTATION,
        "enablePullRequestAutoMerge",
        action="turn on auto-merge",
        expected_head_oid=item.expected_head_oid,
        input={
            "pullRequestId": node["id"],
            "mergeMethod": item.merge_method.upper(),
            "expectedHeadOid": item.expected_head_oid,
        },
    )
    pull_request = payload.get("pullRequest")
    if not isinstance(pull_request, dict) or pull_request.get("id") != node["id"]:
        raise github_failures.unknown_outcome()
    request = pull_request.get("autoMergeRequest")
    enabled_at = request.get("enabledAt") if isinstance(request, dict) else None
    if not isinstance(enabled_at, str) or not enabled_at:
        raise github_failures.unknown_outcome()
    return enabled_at
