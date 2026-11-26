"""A GitHub pull request's merge queue: where it stands, and adding or removing it."""

from __future__ import annotations

from typing import Any

from omnigent.runner.github_graphql import mutate, pull_request_node
from omnigent.runner.pull_request_errors import PullRequestChangedError, UnknownOutcomeError
from omnigent.runner.session_prs import PullRequestRef

_QUEUE_SELECTION = (
    "id headRefOid isMergeQueueEnabled isInMergeQueue mergeQueueEntry { position state }"
)
_ENQUEUE_MUTATION = """mutation($input: EnqueuePullRequestInput!) {
  enqueuePullRequest(input: $input) { mergeQueueEntry { position state } }
}"""
_DEQUEUE_MUTATION = """mutation($input: DequeuePullRequestInput!) {
  dequeuePullRequest(input: $input) { mergeQueueEntry { id } }
}"""


def _answer(
    reference: PullRequestRef,
    node: dict[str, Any],
    entry: dict[str, Any] | None,
    *,
    queued: bool | None,
) -> dict[str, Any]:
    enabled = node.get("isMergeQueueEnabled")
    return {
        "object": "session.github.merge_queue",
        "pr_url": reference.url,
        "enabled": enabled if isinstance(enabled, bool) else None,
        "queued": queued,
        "position": (entry or {}).get("position") if queued else None,
        "state": (entry or {}).get("state") if queued else None,
        "head_sha": node.get("headRefOid"),
    }


def get_merge_queue(root: str, reference: PullRequestRef) -> dict[str, Any]:
    """Whether the pull request's branch has a merge queue, and its place in it."""
    node = pull_request_node(root, reference, _QUEUE_SELECTION)
    queued = node.get("isInMergeQueue")
    return _answer(
        reference,
        node,
        node.get("mergeQueueEntry"),
        queued=queued if isinstance(queued, bool) else None,
    )


def enqueue(root: str, reference: PullRequestRef, *, expected_head_oid: str) -> dict[str, Any]:
    """Add the pull request to its base branch's merge queue, only at ``expected_head_oid``."""
    node = pull_request_node(root, reference, _QUEUE_SELECTION)
    if node.get("headRefOid") != expected_head_oid:
        raise PullRequestChangedError()
    if node.get("isMergeQueueEnabled") is not True:
        raise ValueError("The merge queue isn't turned on for this pull request's branch.")
    if node.get("isInMergeQueue") is True:
        raise ValueError("This pull request is already in the merge queue.")
    # GitHub refuses the mutation itself if the head moves after this read.
    payload = mutate(
        root,
        reference,
        _ENQUEUE_MUTATION,
        "enqueuePullRequest",
        action="add this pull request to the merge queue",
        expected_head_oid=expected_head_oid,
        input={"pullRequestId": node["id"], "expectedHeadOid": expected_head_oid},
    )
    entry = payload.get("mergeQueueEntry")
    if not isinstance(entry, dict):
        # GitHub answered without the entry it made, so the pull request may not be queued.
        raise UnknownOutcomeError()
    return _answer(reference, node, entry, queued=True)


def dequeue(root: str, reference: PullRequestRef) -> dict[str, Any]:
    """Remove the pull request from its merge queue."""
    node = pull_request_node(root, reference, _QUEUE_SELECTION)
    if node.get("isInMergeQueue") is not True:
        raise ValueError("This pull request isn't in the merge queue.")
    # The mutation names the pull request's node, not its queue entry's.
    mutate(
        root,
        reference,
        _DEQUEUE_MUTATION,
        "dequeuePullRequest",
        action="remove this pull request from the merge queue",
        input={"id": node["id"]},
    )
    return _answer(reference, node, None, queued=False)
